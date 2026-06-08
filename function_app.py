import azure.functions as func
import logging
import json
from io import BytesIO
from azure.storage.blob import BlobClient
from azure.storage.filedatalake import DataLakeServiceClient
import ebooklib
from ebooklib import epub
import psycopg2
import os
import requests

app = func.FunctionApp()


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

    # 1순위: manifest properties (ebooklib은 이걸 item.properties로 노출)
    for item in book.get_items():
        if item.get_type() == ebooklib.ITEM_IMAGE:
            if hasattr(item, 'properties') and 'cover-image' in (item.properties or []):
                logging.info(f"표지 발견 (1순위 properties): {item.get_name()}")
                return item.get_content(), item.get_name()

    # 2순위: metadata meta name="cover" → item id로 매핑
    cover_meta = book.get_metadata('OPF', 'cover')  # ebooklib OPF 메타
    if not cover_meta:
        # 직접 파싱 fallback
        cover_meta = book.get_metadata('http://www.idpf.org/2007/opf', 'meta')
    
    # ebooklib으로 OPF meta 직접 접근
    for meta in book.metadata.get('http://www.idpf.org/2007/opf', {}).get('meta', []):
        # meta = (value, {'name': 'cover', 'content': 'item-id'}) 형태
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

    # 4순위: 파일명 cover 포함 이미지 중 용량 최대 (cover.jpg 같은 거 잡을 때)
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
    books_id = None # finally 블록에서 참조하기 위해 함수 시작 시 선언
    
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
        
        # blob_url로 epub 파일 읽기
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

        # epub 파싱
        book = epub.read_epub(epub_bytes)

        # DC 메타데이터: 'DC' 단축키 대신 전체 네임스페이스로
        DC = 'http://purl.org/dc/elements/1.1/'

        raw_title     = book.get_metadata(DC, 'title')
        raw_author    = book.get_metadata(DC, 'creator')
        raw_publisher = book.get_metadata(DC, 'publisher')
        raw_date      = book.get_metadata(DC, 'date')
        raw_identifier= book.get_metadata(DC, 'identifier')

        title_str     = (raw_title     or [['제목 없음']])[0][0].strip()
        author_str    = (raw_author    or [['저자 없음']])[0][0].strip()
        publisher_str = (raw_publisher or [[None]])[0][0]
        year_raw      = (raw_date      or [[None]])[0][0]
        year_str      = year_raw[:4] if year_raw else None
        raw_identifier_val = raw_identifier[0][0] if raw_identifier else None

        # urn:isbn: 형태만 ISBN으로 인정, 나머지는 None
        if raw_identifier_val and 'isbn' in raw_identifier_val.lower():
            # isbn 관련 접두사 전부 제거
            isbn_str = raw_identifier_val.lower()
            isbn_str = isbn_str.replace('urn:isbn:', '').replace('isbn:', '')
            isbn_str = isbn_str.replace('-', '').strip()[:13]
        else:
            isbn_str = None

        # 표지 이미지 - ADLS에 업로드 후 URL 저장
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


        # PostgreSQL에 저장
        conn = psycopg2.connect(
            host=os.environ['PG_HOST'],
            database=os.environ['PG_DATABASE'],
            user=os.environ['PG_USER'],
            password=os.environ['PG_PASSWORD'],
            sslmode='prefer'
        )
        cur = conn.cursor()

        # books 테이블에 책 정보 저장
        cur.execute("""
            INSERT INTO books (title, author, publisher, published_year, cover_url, isbn, admin_id, epub_blob_path)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING books_id
            """, 
            (title_str, author_str, publisher_str, year_str, cover_url, isbn_str, admin_id, blob_url),
        )
        # RETURNING으로 books_id 받아오기
        books_id = cur.fetchone()[0]
        logging.info(f"books 테이블에 저장 완료: books_id={books_id}")

        conn.commit()
        cur.close()


        # 관리자에게 SSE 알림( 실패해도 200 반환)
        _notify(
            event="metadata_done",
            payload={
                "message": "메타데이터 추출 완료, 검수를 진행해주세요 ",
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
        
        _notify(
            event="error",
            payload={
                "step": "metadata_parsing",
                "message": "메타데이터 파싱 중 오류 발생",
                "books_id": books_id,
                "error": str(e)
            },
        )

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
            