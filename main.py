from fastapi import FastAPI
from pydantic import BaseModel
from uuid import uuid4
from datetime import datetime, timedelta

app = FastAPI()

links = {}
evaluations = {}

class GenerateLinkRequest(BaseModel):
    client_id: str
    profession: str

class EvaluateRequest(BaseModel):
    name: str
    email: str
    profession: str
    answers: list
    cv_url: str

@app.get("/health")
def health_check():
    return {"status": "ok"}

@app.post("/generate-link")
def generate_link(data: GenerateLinkRequest):
    link_id = str(uuid4())
    expiry = datetime.utcnow() + timedelta(days=30)
    links[link_id] = {"client_id": data.client_id, "profession": data.profession, "expiry": expiry}
    return {"form_url": f"https://yourdomain.com/form/{link_id}", "expires": expiry}

@app.post("/evaluate")
def evaluate(data: EvaluateRequest):
    fake_score = 85
    evaluations[data.email] = {"score": fake_score, "profession": data.profession}
    return {"score": fake_score, "status": "success"}

@app.get("/report/{client_id}")
def report(client_id: str):
    client_evals = {email: info for email, info in evaluations.items() if info["profession"] == "asztalos"}  
    return {"client_id": client_id, "evaluations": client_evals}
