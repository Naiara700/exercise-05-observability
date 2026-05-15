from datetime import datetime, timezone
from time import perf_counter
from fastapi import Depends, FastAPI, HTTPException, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from sqlalchemy import text
from sqlalchemy.orm import Session
from src.database import Base, engine, get_db
from src.models import Node
from src.schemas import NodeCreate, NodeResponse, NodeUpdate

Base.metadata.create_all(bind=engine)
app = FastAPI()

REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total HTTP requests processed by the API.",
    ["method", "path", "status_code"],
)
REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds.",
    ["method", "path"],
)
ACTIVE_NODES = Gauge(
    "api_active_nodes",
    "Number of active nodes registered in the API.",
)
for method, path, status_code in (
    ("GET", "/health", "200"),
    ("GET", "/metrics", "200"),
    ("POST", "/api/nodes", "201"),
    ("GET", "/api/nodes", "200"),
    ("GET", "/api/nodes/{name}", "200"),
    ("PUT", "/api/nodes/{name}", "200"),
    ("DELETE", "/api/nodes/{name}", "204"),
):
    REQUEST_COUNT.labels(method=method, path=path, status_code=status_code)
    REQUEST_LATENCY.labels(method=method, path=path)
ACTIVE_NODES.set(0)


@app.middleware("http")
async def prometheus_metrics_middleware(request, call_next):
    start = perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        route = request.scope.get("route")
        path = getattr(route, "path", request.url.path)
        method = request.method
        REQUEST_COUNT.labels(method=method, path=path, status_code=str(status_code)).inc()
        REQUEST_LATENCY.labels(method=method, path=path).observe(perf_counter() - start)


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def update_active_nodes_metric(db: Session):
    count = db.query(Node).filter(Node.status == "active").count()
    ACTIVE_NODES.set(count)
    return count


@app.get("/health")
def health(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        db_status = "connected"
        count = update_active_nodes_metric(db)
    except Exception:
        db_status = "disconnected"
        count = 0
        ACTIVE_NODES.set(count)
    return {"status": "ok", "db": db_status, "nodes_count": count}

@app.post("/api/nodes", response_model=NodeResponse, status_code=201)
def register_node(node: NodeCreate, db: Session = Depends(get_db)):
    existing = db.query(Node).filter(Node.name == node.name).first()
    if existing:
        raise HTTPException(status_code=409, detail="Node already exists")
    db_node = Node(name=node.name, host=node.host, port=node.port)
    db.add(db_node)
    db.commit()
    db.refresh(db_node)
    update_active_nodes_metric(db)
    return db_node

@app.get("/api/nodes", response_model=list[NodeResponse])
def list_nodes(db: Session = Depends(get_db)):
    return db.query(Node).all()

@app.get("/api/nodes/{name}", response_model=NodeResponse)
def get_node(name: str, db: Session = Depends(get_db)):
    node = db.query(Node).filter(Node.name == name).first()
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    return node

@app.put("/api/nodes/{name}", response_model=NodeResponse)
def update_node(name: str, update: NodeUpdate, db: Session = Depends(get_db)):
    node = db.query(Node).filter(Node.name == name).first()
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    if update.host is not None:
        node.host = update.host
    if update.port is not None:
        node.port = update.port
    node.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(node)
    update_active_nodes_metric(db)
    return node

@app.delete("/api/nodes/{name}", status_code=204)
def delete_node(name: str, db: Session = Depends(get_db)):
    node = db.query(Node).filter(Node.name == name).first()
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    node.status = "inactive"
    node.updated_at = datetime.now(timezone.utc)
    db.commit()
    update_active_nodes_metric(db)
    return Response(status_code=204)
