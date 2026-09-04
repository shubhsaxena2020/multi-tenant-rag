import json
from app.main import app
from fastapi.openapi.utils import get_openapi

spec = get_openapi(title=app.title, version=app.version, routes=app.routes)
paths = list(spec['paths'].keys())
query_routes = [p for p in paths if 'query' in p.lower()]
for q in sorted(query_routes):
    print(q)