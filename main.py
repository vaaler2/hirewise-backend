import os
import resend
import uuid
import json
import io
import base64
from openai import OpenAI
from pypdf import PdfReader
from datetime import datetime
from typing import List, Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# --- ADATBÁZIS BEÁLLÍTÁSOK ---
from sqlalchemy import create_engine, Column, String, DateTime, Text, Integer, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./hirewise.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Link(Base):
    __tablename__ = "links"
    link_id = Column(String, primary_key=True, index=True)
    client_id = Column(String)
    profession = Column(String)
    company_email = Column(String)
    expires_at = Column(DateTime)
    riport_gyakorisag = Column(String)
    rejtett_leiras = Column(Text)
    extra_kerdesek = Column(Text)

class Application(Base):
    __tablename__ = "applications"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    link_id = Column(String, index=True)
    name = Column(String)
    phone = Column(String)
    email = Column(String)
    about = Column(Text)
    cv_image_path = Column(String)
    submitted_at = Column(DateTime)
    # ÚJ OSZLOPOK AZ AI ÉS A RIPORT SZÁMÁRA:
    score = Column(Integer, default=0)
    ai_evaluation = Column(Text, default="")
    is_reported = Column(Boolean, default=False)

Base.metadata.create_all(bind=engine)

# --- OpenAI és FastAPI beállítások ---
openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY")) if os.getenv("OPENAI_API_KEY") else None
resend.api_key = os.getenv("RESEND_API_KEY")

app = FastAPI(title="Hirewise backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)
CRON_BEARER = os.getenv("CRON_BEARER", "")

class LinkRequest(BaseModel):
    position_name: str
    company_email: str
    riport_gyakorisag: str = "Hetente"
    rejtett_leiras: Optional[str] = ""
    extra_kerdesek: Optional[List[str]] = []

def require_cron_bearer(req: Request):
    auth = req.headers.get("Authorization", "").strip()
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not CRON_BEARER or token != CRON_BEARER.strip():
        raise HTTPException(status_code=401, detail="Unauthorized")

# --- ÚJ AI MOTOR: Egyetlen jelentkezőt értékel azonnal! ---
def _evaluate_single_applicant(app_data: dict, profession: str, rejtett_leiras: str):
    if openai_client is None:
        return 0, "AI nem elérhető, helyi mentés történt."
    
    leiras = rejtett_leiras if rejtett_leiras else "Nincsenek extra elvárások megadva."
    
    prompt = (
        f"Te egy profi HR asszisztens vagy. A cég ehhez a pozícióhoz keres embert: '{profession}'.\n"
        f"A cégvezető extra elvárásai (rejtett leírás): '{leiras}'.\n\n"
        "Kérlek, értékeld a jelentkezőt 1-től 100-ig, hogy mennyire felel meg EZEKNEK a követelményeknek! "
        "Légy szigorú: ha hiányzik egy elvárt készség, vonj le pontot.\n"
        "KIZÁRÓLAG érvényes JSON formátumban válaszolj, a következő kulcsokkal:\n"
        "'pontszam' (szám 1 és 100 között), 'indoklas' (maximum 3 mondatos rövid összefoglaló).\n\n"
        f"A jelentkező adatai:\n"
        f"- Név: {app_data.get('name', '')}\n"
        f"- Önéletrajz (és bemutatkozás): {app_data.get('about', '')}\n"
    )
    
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={ "type": "json_object" },
            messages=[
                {"role": "system", "content": "Te egy kíméletlen, profi HR szakértő vagy."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
        )
        result_data = json.loads(resp.choices[0].message.content)
        return int(result_data.get("pontszam", 0)), result_data.get("indoklas", "")
    except Exception as e:
        print(f"AI értékelési hiba: {e}")
        return 0, "Hiba az AI értékelés során."


# ---------- ENDPOINTOK ----------

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/generate-link")
def generate_link(data: LinkRequest):
    link_id = str(uuid.uuid4())[:8]
    db = SessionLocal()
    try:
        new_link = Link(
            link_id=link_id,
            profession=data.position_name,
            company_email=data.company_email,
            riport_gyakorisag=data.riport_gyakorisag,
            rejtett_leiras=data.rejtett_leiras,
            extra_kerdesek=json.dumps(data.extra_kerdesek)
        )
        db.add(new_link)
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()
    return {"success": True, "link_id": link_id, "url": f"https://hirewise-ai.bolt.host/apply/{link_id}"}


@app.post("/submit-form/{link_id}")
def submit_form(
    link_id: str,
    name: str = Form(...),
    phone: str = Form(...),
    email: str = Form(...),
    about: str = Form(...),
    cv_image: UploadFile = File(...)
):
    db = SessionLocal()
    link_record = db.query(Link).filter(Link.link_id == link_id).first()
    if not link_record:
        db.close()
        raise HTTPException(status_code=404, detail="Invalid link")

    ext = (cv_image.filename or "bin").split(".")[-1].lower()
    file_name = f"{uuid.uuid4()}.{ext}"
    file_path = os.path.join(UPLOAD_DIR, file_name)
    
    content = cv_image.file.read()
    with open(file_path, "wb") as f:
        f.write(content)

    cv_text = ""
    if ext == "pdf":
        try:
            reader = PdfReader(io.BytesIO(content))
            for page in reader.pages:
                extracted = page.extract_text()
                if extracted:
                    cv_text += extracted + "\n"
        except Exception as e:
            print(f"PDF hiba: {e}")

    final_about = about
    if cv_text.strip():
        final_about += f"\n\n--- DOKUMENTUM TARTALMA ---\n{cv_text.strip()}"

    # AZONNALI AI ÉRTÉKELÉS BEKÖTÉSE!
    score, ai_eval = _evaluate_single_applicant(
        {"name": name, "about": final_about},
        link_record.profession,
        link_record.rejtett_leiras
    )

    new_app = Application(
        link_id=link_id,
        name=name,
        phone=phone,
        email=email,
        about=final_about,
        cv_image_path=file_path,
        submitted_at=datetime.utcnow(),
        score=score,
        ai_evaluation=ai_eval,
        is_reported=False  # Még nem küldtük el riportban
    )
    db.add(new_app)
    db.commit()
    db.close()

    return {"message": "Application submitted successfully"}


@app.post("/tasks/send_weekly_reports")
def send_weekly_reports(request: Request):
    require_cron_bearer(request)
    db = SessionLocal()
    links = db.query(Link).all()
    
    sent_count = 0
    for link in links:
        # CSAK AZOKAT KÉRJÜK LE, AKIKET MÉG NEM JELENTETTÜNK (is_reported == False), ÉS RENDEZZÜK PONTSZÁM SZERINT (DESC)!
        apps = db.query(Application).filter(
            Application.link_id == link.link_id,
            Application.is_reported == False
        ).order_by(Application.score.desc()).all()
        
        if not apps:
            continue
            
        html_content = f"""
        <div style="font-family: Arial, sans-serif; color: #333; max-width: 600px; margin: 0 auto;">
            <h2 style="color: #2563eb; border-bottom: 2px solid #2563eb; padding-bottom: 10px;">HireWise AI – Friss Jelölt Riport</h2>
            <p>Itt vannak az <b>új jelentkezők</b> a(z) <b style="color: #2563eb;">{link.profession}</b> pozícióra, alkalmassági sorrendben:</p>
            <table border="0" cellpadding="10" cellspacing="0" style="width: 100%; border-collapse: collapse; margin-top: 20px;">
                <tr style="background-color: #2563eb; color: white; text-align: left;">
                    <th>Psz.</th>
                    <th>Név / Elérhetőség</th>
                    <th>AI Indoklás</th>
                </tr>
        """
        
        for a in apps:
            score_color = "#10b981" if a.score >= 70 else ("#f59e0b" if a.score >= 50 else "#ef4444")
            html_content += f"""
                <tr style="border-bottom: 1px solid #e5e7eb;">
                    <td style="font-size: 20px; font-weight: bold; color: {score_color}; text-align: center;">
                        {a.score}
                    </td>
                    <td style="font-size: 14px;">
                        <b>{a.name}</b><br>
                        <span style="color: #6b7280; font-size: 12px;">{a.email}<br>{a.phone}</span>
                    </td>
                    <td style="font-size: 13px; color: #4b5563;">
                        {a.ai_evaluation}
                    </td>
                </tr>
            """
            # Pipáljuk ki, hogy el lett küldve!
            a.is_reported = True
            
        html_content += "</table></div>"
        
        try:
            resend.Emails.send({
                "from": "onboarding@resend.dev",
                "to": link.company_email,
                "subject": f"🔥 AI Rangsorolt Jelölt Riport: {link.profession}",
                "html": html_content
            })
            sent_count += 1
            db.commit()  # Mentsük el a státusz változást
        except Exception as e:
            db.rollback()
            print(f"Hiba küldéskor: {e}")
            
    db.close()
    return {"ok": True, "sent_emails": sent_count}
