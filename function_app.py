import azure.functions as func
import logging
import json
import zipfile
from io import BytesIO
from azure.storage.blob import BlobClient
from azure.storage.filedatalake import DataLakeServiceClient
import ebooklib
from ebooklib import epub
import psycopg2
import os
import requests

app = func.FunctionApp()

# 환경변수 누락 체크
REQUIRED_ENV_VARS = [
    'ADLS_ACCOUNT_KEY',
    'ADLS_ACCOUNT_NAME',
    'PG_HOST',
    'PG_DATABASE',
    'PG_USER',
    'PG_PASSWORD',
]

def check_env_vars() -> list[str]:
    """누락된 환경변수 목록 반환"""
    return [var for var in REQUIRED_ENV_VARS if not os.environ.get(var)]


# epub 유효성 검사
def validate_epub(epub_bytes: BytesIO) -> tuple[bool, str]:
    """
    epub 파일 유효성 검사
    - zip 구조 검사
    - mimetype 검사
    반환: (is_valid, error_message)
    """
    epub_bytes.seek(0)
    
    # 1. zip 구조 검사
    try:
        with zipfile.ZipFile(epub_bytes) as zf:
            names = zf.namelist()
            
            # 2. mimetype 파일 존재 여부
            if 'mimetype' not in names:
                return False, "유효하지 않은 epub 파일입니다: mimetype 파일 없음"
            
            # 3. mimetype 내용 검사
            mimetype_content = zf.read('mimetype').decode('utf-8', errors='ignore').strip()
            if mimetype_content != 'application/epub+zip':
                return False, f"유효하지 않은 epub 파일입니다: mimetype={mimetype_content}"
            
            # 4. 필수 구조 파일 검사 (OPF)
            has_opf = any(name.endswith('.opf') for name in names)
            if not has_opf:
                return False, "유효하지 않은 epub 파일입니다: OPF 파일 없음"
                
    except zipfile.BadZipFile:
        return False, "유효하지 않은 epub 파일입니다: zip 구조 손상 (파일 내부가 비어있거나 손상됨)"
    except Exception as e:
        return False, f"epub 파일 검사 중 오류: {str(e)}"
    
    epub_bytes.seek(0)
    return True, ""


# 표지 이미지 추출
def get_cover_image(book: epub.EpubBook) -> tuple[bytes, str] | tuple[None, None]:
    """
    우선순위:
    1. manifest properties="cover-image"
    2. metadata <meta name="cover">
    3. ebooklib ITEM_COVER 타입
    4. 파일명 cover 포함 이미지 중 용량 최대
    """
    images = {
        item.get_name(): item
        for item in book.get_items()
        if item.get_type() == ebooklib.ITEM_IMAGE
    }

    # 1순위: manifest properties
    for item in book.get_items():
        if item.get_type() == ebooklib.ITEM_IMAGE:
            if hasattr(item, 'properties') and 'cover-image' in (item.properties or []):
                logging.info(f"표지 발견 (1순위 properties): {item.get_name()}")
                return item.get_content(), item.get_name()

    # 2순위: metadata meta name="cover"
    for meta in book.metadata.get('http://www.idpf.org/2007/opf', {}).get('meta', []):
        if isinstance(meta, tuple) and len(meta) > 1:
            attrs = meta[1] if isinstance(meta[1], dict) else {}
            if attrs.get('name') == 'cover':
                cover_id = attrs.get('content', '')
                item = book.get_item_with_id(cover_id)
                if item and item.get_type() == ebooklib.ITEM_IMAGE:
                    logging.info(f"표지 발견 (2순위 meta): {item.get_name()}")
                    return item.get_content(), item.get_name()

    # 3순위: ebooklib ITEM_COVER
    for item in book.get_items():
        if item.get_type() == ebooklib.ITEM_COVER:
            logging.info(f"표지 발견 (3순위 ITEM_COVER): {item.get_name()}")
            return item.get_content(), item.get_name()

    # 4순위: 파일명 cover 포함 이미지 중 용량 최대
    cover_candidates = [
        item for name, item in images.items()
        if 'cover' in name.lower()
    ]
    if cover_candidates:
        best = max(cover_candidates, key=lambda i: len(i.get_content()))
        logging.info(f"표지 발견 (4순위 파일명): {best.get_name()}")
        return best.get_content(), best.get_name()

    logging.warning("표지 이미지 없음")
    return None, None


# 메인 함수
@app.route(route="metadata_parser", methods=["POST"])
def metadata_parser(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('메타데이터 파싱 함수 시작')

    conn = None
    books_id = None
    cover_filename = None  # DB 실패 시 ADLS에서 삭제

    # 1. 환경변수 누락 체크
    missing_vars = check_env_vars()
    if missing_vars:
        msg = f"환경변수 누락: {', '.join(missing_vars)}"
        logging.error(msg)
        return func.HttpResponse(
            json.dumps({"error": msg}),
            status_code=500,
            mimetype="application/json"
        )

    try:
        req_body = req.get_json()
        blob_url = req_body.get('file_url')
        admin_id = req_body.get('admin_id')

        if not blob_url:
            return func.HttpResponse(
                json.dumps({"error": "file_url 없음"}),
                status_code=400,
                mimetype="application/json"
            )

        if not admin_id:
            return func.HttpResponse(
                json.dumps({"error": "admin_id 없음"}),
                status_code=400,
                mimetype="application/json"
            )

        # 2. Blob 다운로드
        blob_client = BlobClient.from_blob_url(
            blob_url=blob_url,
            credential=os.environ['ADLS_ACCOUNT_KEY']
        )

        epub_filename = blob_url.rstrip("/").split("/")[-1]
        epub_name_wo_ext = os.path.splitext(epub_filename)[0]

        adls_client = DataLakeServiceClient(
            account_url=f"https://{os.environ['ADLS_ACCOUNT_NAME']}.dfs.core.windows.net",
            credential=os.environ['ADLS_ACCOUNT_KEY']
        )

        epub_bytes = BytesIO(blob_client.download_blob().readall())
        epub_bytes.seek(0)
        logging.info(f"EPUB 파일 다운로드 완료: {blob_url}")

        # 3. epub 유효성 검사
        is_valid, validation_error = validate_epub(epub_bytes)
        if not is_valid:
            logging.warning(f"epub 유효성 검사 실패: {validation_error}")
            _notify(
                event="error",
                payload={
                    "step": "epub_validation",
                    "message": validation_error,
                    "error": validation_error
                }
            )
            return func.HttpResponse(
                json.dumps({"error": validation_error}),
                status_code=400,
                mimetype="application/json"
            )

        # 4. epub 파싱
        book = epub.read_epub(epub_bytes)
        DC = 'http://purl.org/dc/elements/1.1/'

        raw_title     = book.get_metadata(DC, 'title')
        raw_author    = book.get_metadata(DC, 'creator')
        raw_publisher = book.get_metadata(DC, 'publisher')
        raw_date      = book.get_metadata(DC, 'date')
        raw_identifier = book.get_metadata(DC, 'identifier')

        title_str     = (raw_title     or [['제목 없음']])[0][0].strip()
        author_str    = (raw_author    or [['저자 없음']])[0][0].strip()
        publisher_str = (raw_publisher or [[None]])[0][0]
        year_raw      = (raw_date      or [[None]])[0][0]
        year_str      = year_raw[:4] if year_raw else None
        raw_identifier_val = raw_identifier[0][0] if raw_identifier else None

        if raw_identifier_val and 'isbn' in raw_identifier_val.lower():
            isbn_str = raw_identifier_val.lower()
            isbn_str = isbn_str.replace('urn:isbn:', '').replace('isbn:', '')
            isbn_str = isbn_str.replace('-', '').strip()[:13]
        else:
            isbn_str = None

        # 5. 표지 이미지 업로드
        cover_url = None
        cover_bytes, cover_origin_name = get_cover_image(book)
        if cover_bytes:
            ext = os.path.splitext(cover_origin_name)[1] or '.jpg'
            cover_filename = f"{epub_name_wo_ext}_cover{ext}"
            cover_client = (
                adls_client
                .get_file_system_client("cover")
                .get_file_client(cover_filename)
            )
            cover_client.upload_data(cover_bytes, overwrite=True)
            cover_url = (
                f"https://{os.environ['ADLS_ACCOUNT_NAME']}.blob.core.windows.net"
                f"/cover/{cover_filename}"
            )
            logging.info(f"표지 업로드 완료: {cover_url}")

        # 6. PostgreSQL 저장
        conn = psycopg2.connect(
            host=os.environ['PG_HOST'],
            database=os.environ['PG_DATABASE'],
            user=os.environ['PG_USER'],
            password=os.environ['PG_PASSWORD'],
            sslmode='prefer'
        )
        cur = conn.cursor()

        # epub_blob_path 중복 시 기존 row 반환
        cur.execute("""
            INSERT INTO books (title, author, publisher, published_year, cover_url, isbn, admin_id, epub_blob_path)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (epub_blob_path) DO UPDATE
                SET updated_at = CURRENT_TIMESTAMP
            RETURNING books_id, (xmax = 0) AS inserted
        """,
            (title_str, author_str, publisher_str, year_str, cover_url, isbn_str, admin_id, blob_url),
        )

        row = cur.fetchone()
        books_id = row[0]
        is_new_insert = row[1]

        if is_new_insert:
            logging.info(f"books 테이블 신규 저장: books_id={books_id}")
        else:
            logging.warning(f"중복 epub_blob_path - 기존 row 반환: books_id={books_id}")

        conn.commit()
        cur.close()

        # DB 성공 후 cover_filename 초기화
        cover_filename = None

        # 7. 완료 알림
        _notify(
            event="metadata_done",
            payload={
                "message": "메타데이터 추출 완료, 검수를 진행해주세요",
                "books_id": books_id,
                "title": title_str,
                "author": author_str
            },
        )

        return func.HttpResponse(
            json.dumps({
                "status": "success",
                "books_id": books_id,
                "title": title_str,
                "author": author_str,
                "publisher": publisher_str,
                "published_year": year_str,
                "isbn": isbn_str
            }),
            status_code=200,
            mimetype="application/json"
        )

    except Exception as e:
        logging.error(f"오류 발생: {e}", exc_info=True)
        if conn:
            conn.rollback()

        # 커버 고아 방지: DB 실패 시 ADLS 커버 삭제
        if cover_filename:
            try:
                adls_client = DataLakeServiceClient(
                    account_url=f"https://{os.environ['ADLS_ACCOUNT_NAME']}.dfs.core.windows.net",
                    credential=os.environ['ADLS_ACCOUNT_KEY']
                )
                adls_client.get_file_system_client("cover").get_file_client(cover_filename).delete_file()
                logging.info(f"고아 커버 이미지 삭제 완료: {cover_filename}")
            except Exception as cleanup_err:
                logging.warning(f"커버 이미지 삭제 실패 (무시): {cleanup_err}")

        # books_id 있을 때만 payload에 포함
        error_payload = {
            "step": "metadata_parsing",
            "message": "메타데이터 파싱 중 오류 발생",
            "error": str(e)
        }
        if books_id is not None:
            error_payload["books_id"] = books_id

        _notify(event="error", payload=error_payload)

        return func.HttpResponse(
            json.dumps({"error": str(e)}),
            status_code=500,
            mimetype="application/json"
        )
    finally:
        if conn:
            conn.close()


# Webhook 헬퍼
def _notify(event: str, payload: dict) -> None:
    webhook_url = os.environ.get("WEBHOOK_URL")
    if not webhook_url:
        return
    try:
        requests.post(
            webhook_url,
            json={"event": event, **payload},
            timeout=10,
        )
    except Exception as ex:
        logging.warning(f"Webhook 알림 실패 (무시): {ex}")