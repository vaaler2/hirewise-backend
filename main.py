import os
import resend
import uuid
import json
import io
import base64
from openai import OpenAI
from pypdf import PdfReader
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from fastapi.responses import FileResponse

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
    last_reported_at = Column(DateTime, default=datetime.utcnow)

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

@app.get("/download/{filename}")
def download_file(filename: str):
    file_path = os.path.join(UPLOAD_DIR, filename)
    if os.path.exists(file_path):
        return FileResponse(file_path)
    raise HTTPException(status_code=404, detail="A fájl nem található, vagy biztonsági okokból már törlésre került.")

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
            extra_kerdesek=json.dumps(data.extra_kerdesek),
            last_reported_at=datetime.utcnow()
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
        is_reported=False
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
    now = datetime.utcnow()
    
    for link in links:
        gyakorisag = link.riport_gyakorisag.lower() if link.riport_gyakorisag else "hetente"
        if "3" in gyakorisag:
            days_to_wait = 3
        elif "nap" in gyakorisag:
            days_to_wait = 1
        else:
            days_to_wait = 7
            
        last_rep = link.last_reported_at
        if not last_rep:
            last_rep = now - timedelta(days=days_to_wait)
            
        if now < last_rep + timedelta(days=days_to_wait):
            continue
            
        all_apps = db.query(Application).filter(
            Application.link_id == link.link_id
        ).order_by(Application.score.desc()).all()
        
        has_new = any(not a.is_reported for a in all_apps)
        
        if not has_new or not all_apps:
            continue
            
        # --- ÚJ KOMPAKT DIZÁJN ---
        html_content = f"""
        <div style="font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; max-width: 750px; margin: 0 auto; background-color: #f3f4f6; padding: 20px;">
            <div style="text-align: center; padding-bottom: 20px;">
                <h2 style="color: #1e3a8a; margin: 0; font-size: 24px;">HireWise AI Riport</h2>
                <p style="color: #6b7280; font-size: 15px; margin-top: 5px;">Aktuális, teljes rangsor a(z) <b>{link.profession}</b> pozícióra</p>
            </div>
        """
        
        for index, a in enumerate(all_apps):
            score_color = "#10b981" if a.score >= 70 else ("#f59e0b" if a.score >= 50 else "#ef4444")
            new_badge = '<span style="background-color: #ef4444; color: white; font-size: 10px; padding: 2px 6px; border-radius: 8px; margin-left: 8px; font-weight: bold; vertical-align: middle;">ÚJ!</span>' if not a.is_reported else ''
            
            filename = os.path.basename(a.cv_image_path) if a.cv_image_path else ""
            download_url = f"https://hirewise-backend-nn33.onrender.com/download/{filename}" if filename else "#"

            html_content += f"""
            <div style="background-color: #ffffff; border-radius: 6px; padding: 15px; margin-bottom: 12px; border-left: 5px solid {score_color}; box-shadow: 0 1px 3px rgba(0,0,0,0.1);">
                <table width="100%" border="0" cellpadding="0" cellspacing="0">
                    <tr>
                        <td valign="top" style="width: 70px; text-align: center; padding-right: 15px;">
                            <div style="font-size: 11px; color: #9ca3af; font-weight: bold; text-transform: uppercase;">#{index + 1}</div>
                            <div style="color: {score_color}; font-weight: bold; font-size: 22px; margin-top: 4px;">{a.score}</div>
                            <div style="font-size: 11px; color: #6b7280; margin-top: 2px;">/ 100</div>
                        </td>
                        <td valign="top" style="padding-right: 15px;">
                            <h3 style="margin: 0 0 4px 0; color: #111827; font-size: 16px;">{a.name} {new_badge}</h3>
                            <p style="margin: 0 0 8px 0; color: #6b7280; font-size: 12px;">
                                <a href="mailto:{a.email}" style="color: #2563eb; text-decoration: none;">{a.email}</a> • {a.phone}
                            </p>
                            <p style="margin: 0; color: #4b5563; font-size: 13px; line-height: 1.4;">
                                {a.ai_evaluation}
                            </p>
                        </td>
                        <td valign="middle" style="width: 100px; text-align: right;">
                            <a href="{download_url}" target="_blank" style="display: inline-block; background-color: #f3f4f6; color: #374151; text-decoration: none; padding: 8px 10px; border-radius: 4px; font-weight: bold; font-size: 11px; border: 1px solid #d1d5db; white-space: nowrap;">
                                📄 Önéletrajz
                            </a>
                        </td>
                    </tr>
                </table>
            </div>
            """
            a.is_reported = True
            
        html_content += """
            <div style="text-align: center; padding-top: 10px;">
                <p style="color: #9ca3af; font-size: 12px;">Ezt az üzenetet a HireWise AI automatikusan generálta.</p>
            </div>
        </div>
        """
        
        try:
            resend.Emails.send({
                "from": "onboarding@resend.dev",
                "to": link.company_email,
                "subject": f"🏆 Aktuális Jelölt Ranglista: {link.profession}",
                "html": html_content
            })
            sent_count += 1
            link.last_reported_at = now
            db.commit() 
            
        except Exception as e:
            db.rollback()
            print(f"Hiba küldéskor: {e}")
            
    db.close()
    return {"ok": True, "sent_emails": sent_count}

@app.post("/tasks/cleanup_gdpr")
def cleanup_gdpr(request: Request):
    require_cron_bearer(request)
    db = SessionLocal()
    
    sixty_days_ago = datetime.utcnow() - timedelta(days=60)
    expired_links = db.query(Link).filter(Link.last_reported_at < sixty_days_ago).all()
    
    deleted_links_count = 0
    deleted_apps_count = 0
    deleted_files_count = 0
    
    for link in expired_links:
        apps = db.query(Application).filter(Application.link_id == link.link_id).all()
        for app in apps:
            if app.cv_image_path and os.path.exists(app.cv_image_path):
                try:
                    os.remove(app.cv_image_path)
                    deleted_files_count += 1
                except Exception as e:
                    print(f"Hiba a fájl törlésekor: {app.cv_image_path} - {e}")
            db.delete(app)
            deleted_apps_count += 1
        db.delete(link)
        deleted_links_count += 1
        
    db.commit()
    db.close()
    
    return {
        "ok": True, 
        "message": "GDPR Tisztítás sikeresen lefutott!",
        "deleted_links": deleted_links_count,
        "deleted_applications": deleted_apps_count,
        "deleted_files_count": deleted_files_count
    }
