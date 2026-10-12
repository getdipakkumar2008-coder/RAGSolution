import json
import logging

import azure.functions as func

from rag import InvalidInput, get_service


app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)


def json_response(body, status=200):
    return func.HttpResponse(
        json.dumps(body, ensure_ascii=False), status_code=status,
        mimetype="application/json",
    )


@app.route(route="documents", methods=["POST"])
def ingest_document(req: func.HttpRequest) -> func.HttpResponse:
    try:
        result = get_service().ingest(
            req.get_body(), req.params.get("filename", ""),
            req.headers.get("content-type", "").split(";")[0].strip().lower(),
        )
        return json_response(result, 201)
    except InvalidInput as error:
        return json_response({"error": str(error)}, 400)
    except Exception:
        logging.warning("Document ingestion failed.")
        return json_response({"error": "Document ingestion failed."}, 503)


@app.route(route="query", methods=["POST"])
def query_documents(req: func.HttpRequest) -> func.HttpResponse:
    try:
        if len(req.get_body()) > 16 * 1024:
            raise InvalidInput("Query request exceeds 16 KiB.")
        try:
            body = req.get_json()
        except ValueError:
            raise InvalidInput("Request body must be a JSON object.") from None
        if not isinstance(body, dict):
            raise InvalidInput("Request body must be a JSON object.")
        return json_response(get_service().ask(body.get("question"), body.get("top_k", 5)))
    except InvalidInput as error:
        return json_response({"error": str(error)}, 400)
    except Exception:
        logging.warning("RAG query failed.")
        return json_response({"error": "Query failed."}, 503)
