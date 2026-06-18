import os
import resend
import uuid
import json
import io
import base64
from openai import OpenAI
from pypdf import PdfReader
import shutil
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# --- ADATBÁZIS BEÁLLÍTÁSOK (SQLAlchemy) ---
from sqlalchemy import create_engine, Column, String, DateTime, Text
from sqlalchemy.orm import declarative_base, sessionmaker

# A Neon linket a Render Env-ből olvassuk ki (ha nincs, egy helyi sqlite-ot próbál)
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./hirewise.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# --- ADATBÁZIS TÁBLÁK (MODELEK) ---
class Link(Base):
    __tablename__ = "links"
    link_id = Column(String, primary_key=True, index=True)
    client_id = Column(String)
    profession = Column(String)
    company_email = Column(String)
    expires_at = Column(DateTime)

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

# Táblák létrehozása (ha még nincsenek meg)
Base.metadata.create_all(bind=engine)

# --- OpenAI kliens ---
try:
    from openai import OpenAI
    openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
except Exception:
    openai_client = None

app = FastAPI(title="Hirewise backend")
# CORS engedélyezése, hogy a weboldalunk tudjon beszélni a szerverrel
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Fejlesztés alatt mindent átengedünk, később ide írjuk a weblapod címét
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)
CRON_BEARER = os.getenv("CRON_BEARER", "")


# ---------- MODELS ----------

   # Ezt a struktúrát várjuk a weblaptól
class LinkRequest(BaseModel):
    position_name: str
    riport_gyakorisag: str = "Hetente"
    rejtett_leiras: Optional[str] = ""
    extra_kerdesek: Optional[List[str]] = []

# ---------- SEGÉDFÜGGVÉNYEK ----------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def require_cron_bearer(req: Request):
    auth = req.headers.get("Authorization", "").strip()
    token = ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    if not CRON_BEARER or token != CRON_BEARER.strip():
        raise HTTPException(status_code=401, detail="Unauthorized")

def _local_score(app_dict: dict, profession: str) -> float:
    text = (app_dict.get("about") or "").lower()
    name_ok = 1 if app_dict.get("name") else 0
    phone_ok = 1 if app_dict.get("phone") else 0
    email_ok = 1 if app_dict.get("email") else 0
    len_bonus = min(len(text) / 400, 1)
    prof_hit = 1 if profession.lower() in text else 0

    kw = {
        "asztalos": ["fa", "bútor", "marás", "csiszolás", "gépek"],
        "software developer": ["python", "java", "react", "docker", "api"],
    }
    kws = kw.get(profession.lower(), [])
    kw_hits = sum(1 for k in kws if k in text)
    kw_score = min(kw_hits / 3, 1)

    raw = 2 * prof_hit + 2 * kw_score + 1.5 * len_bonus + name_ok + phone_ok + email_ok
    return round(100 * raw / 7.5, 1)

def _ai_evaluate(applications: List[dict], profession: str):
    if openai_client is None:
        raise RuntimeError("OpenAI kliens nem elérhető")

    # Itt utasítjuk az AI-t a szigorú JSON formátumra és a kíméletlen pontozásra
    prompt = (
        "Értékeld az alábbi jelentkezőket a megadott szakma alapján. "
        "FIGYELEM: Légy rendkívül szigorú! Ha a jelentkező önéletrajzában vagy bemutatkozásában nincs a megadott pozícióhoz szorosan kapcsolódó konkrét szakmai tapasztalat, tanulmány vagy szoftveres/technikai ismeret, adj maximum 1 vagy 2 pontot, függetlenül attól, hogy más területen milyen jó vagy szorgalmas munkaerő. "
        "KIZÁRÓLAG érvényes JSON formátumban válaszolj! "
        "A JSON egy 'results' nevű listát tartalmazzon, amiben minden jelentkezőnek van egy objektuma a következő kulcsokkal: "
        "'nev' (a jelentkező neve), 'pontszam' (1-10 közötti szám), 'indoklas' (rövid szöveges értékelés).\n\n"
        f"Szakma: {profession}\n\n"
    )
    
    for i, app_data in enumerate(applications, 1):
        prompt += (
            f"Jelentkező {i}:\n"
            f"- Név: {app_data.get('name','')}\n"
            f"- Telefon: {app_data.get('phone','')}\n"
            f"- E-mail: {app_data.get('email','')}\n"
            f"- Bemutatkozás: {app_data.get('about','')}\n\n"
        )

    resp = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        response_format={ "type": "json_object" }, # <--- ETTŐL LESZ JSON MÓD!
        messages=[
            {"role": "system", "content": "Te egy profi HR asszisztens vagy."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
    )
    
    # Itt alakítjuk át a szöveget igazi Python adatszerkezetté!
    # Itt alakítjuk át a szöveget igazi Python adatszerkezetté!
    import json
    result_text = resp.choices[0].message.content
    result_data = json.loads(result_text)
    
    # RANGSOROLÁS: A Python sorba rendezi a listát pontszám alapján (legjobb elöl)
    if "results" in result_data:
        result_data["results"].sort(key=lambda x: x.get("pontszam", 0), reverse=True)
        
    return result_data


# ---------- ENDPOINTOK ----------
@app.get("/health")
def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}


@app.post("/generate-link")
def generate_link(data: LinkRequest):
    import uuid
    link_id = str(uuid.uuid4())[:8] # Generálunk egy 8 karakteres azonosítót

    # Csatlakozás a Neon adatbázishoz
    conn = get_db_connection()
    cur = conn.cursor()

    # A kérdések listáját szöveggé (JSON string) alakítjuk a mentéshez
    kerdesek_json = json.dumps(data.extra_kerdesek)

    cur.execute("""
        INSERT INTO links (link_id, position_name, riport_gyakorisag, rejtett_leiras, extra_kerdesek)
        VALUES (%s, %s, %s, %s, %s)
    """, (link_id, data.position_name, data.riport_gyakorisag, data.rejtett_leiras, kerdesek_json))

    conn.commit()
    cur.close()
    conn.close()

    # Visszaküldjük a weblapnak a sikeres linket!
    return {
        "success": True, 
        "link_id": link_id, 
        "url": f"https://hirewise-backend-nn33.onrender.com/apply/{link_id}"
    }

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

    # Fájl kiterjesztésének lekérése (pl. 'pdf', 'jpg')
    ext = (cv_image.filename or "bin").split(".")[-1].lower()
    file_name = f"{uuid.uuid4()}.{ext}"
    file_path = os.path.join(UPLOAD_DIR, file_name)
    
    # Fájl tartalmának beolvasása a memóriába
    content = cv_image.file.read()
    
    # Fájl elmentése a szerverre
    with open(file_path, "wb") as f:
        f.write(content)

    # PDF és Kép szöveg kinyerése
    cv_text = ""
    if ext == "pdf":
        try:
            reader = PdfReader(io.BytesIO(content))
            for page in reader.pages:
                extracted = page.extract_text()
                if extracted:
                    cv_text += extracted + "\n"
        except Exception as e:
            print(f"PDF olvasási hiba: {e}")
            
    elif ext in ["jpg", "jpeg", "png"]:
        try:
            # A képet átalakítjuk Base64 szöveggé
            import base64
            from openai import OpenAI
            
            base64_image = base64.b64encode(content).decode('utf-8')
            mime_type = "image/jpeg" if ext in ["jpg", "jpeg"] else "image/png"
            
            client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Kérlek olvasd el ezt az önéletrajzot/dokumentumot és másold ki a benne lévő teljes szöveget. Ne fűzz hozzá semmilyen megjegyzést, csak a kinyert szöveget add vissza!"},
                            {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{base64_image}"}}
                        ]
                    }
                ],
                max_tokens=1000
            )
            cv_text = response.choices[0].message.content
        except Exception as e:
            print(f"Kép AI olvasási hiba: {e}")

    # Ha találtunk szöveget (akár PDF, akár JPG), hozzáfűzzük a rövid bemutatkozáshoz
    final_about = about
    if cv_text and cv_text.strip():
        final_about += f"\n\n--- DOKUMENTUM TARTALMA ---\n{cv_text.strip()}"

    # Adatok mentése az adatbázisba az új, kibővített szöveggel
    new_app = Application(
        link_id=link_id,
        name=name,
        phone=phone,
        email=email,
        about=final_about,
        cv_image_path=file_path,
        submitted_at=datetime.utcnow()
    )
    db.add(new_app)
    db.commit()
    db.close()

    return {"message": "Application submitted successfully"}


@app.get("/applications/{link_id}")
def get_applications(link_id: str):
    db = SessionLocal()
    link_record = db.query(Link).filter(Link.link_id == link_id).first()
    if not link_record:
        db.close()
        raise HTTPException(status_code=404, detail="Invalid link")

    app_records = db.query(Application).filter(Application.link_id == link_id).all()
    profession = link_record.profession
    
    apps_list = []
    for a in app_records:
        apps_list.append({
            "name": a.name,
            "phone": a.phone,
            "email": a.email,
            "about": a.about,
            "cv_image_path": a.cv_image_path,
            "submitted_at": a.submitted_at.isoformat()
        })
    
    db.close()

    if not apps_list:
        return {
            "link_id": link_id,
            "client_id": link_record.client_id,
            "profession": profession,
            "company_email": link_record.company_email,
            "applications": [],
            "evaluation": "No applications yet",
        }

    try:
        evaluation = _ai_evaluate(apps_list, profession)
        return {
            "link_id": link_id,
            "client_id": link_record.client_id,
            "profession": profession,
            "company_email": link_record.company_email,
            "applications": apps_list,
            "evaluation": evaluation,
        }
    except Exception as e:
        for a in apps_list:
            a["score"] = _local_score(a, profession)
        apps_list.sort(key=lambda x: x.get("score", 0), reverse=True)
        for i, a in enumerate(apps_list, 1):
            a["rank"] = i
        evaluation = f"AI értékelés nem elérhető ({e}); helyi pontozás és rangsor látható."

        return {
            "link_id": link_id,
            "client_id": link_record.client_id,
            "profession": profession,
            "company_email": link_record.company_email,
            "applications": apps_list,
            "evaluation": evaluation,
        }

# A Resend kulcs beállítása a környezeti változókból
resend.api_key = os.getenv("RESEND_API_KEY")

@app.post("/tasks/send_weekly_reports")
def send_weekly_reports(request: Request):
    require_cron_bearer(request)
    
    db = SessionLocal()
    # Lekérjük az összes élő pozíció linket az adatbázisból
    links = db.query(Link).all()
    
    sent_count = 0
    for link in links:
        # Megkeressük az adott linkhez tartozó jelentkezőket
        apps = db.query(Application).filter(Application.link_id == link.link_id).all()
        
        if not apps:
            continue  # Ha nincs jelentkező, nem küldünk üres e-mailt
            
        # Átalakítjuk a jelentkezőket olyan listává, amit az AI függvényünk vár
        apps_list = []
        for a in apps:
            apps_list.append({
                "name": a.name,
                "phone": a.phone,
                "email": a.email,
                "about": a.about
            })
            
        try:
            # Megkérjük az AI-t, hogy pontozza és RANGSOROLJA a listát
            ai_data = _ai_evaluate(apps_list, link.profession)
            results = ai_data.get("results", [])
        except Exception as e:
            print(f"Hiba az e-mail AI értékelésnél: {e}")
            results = []

        # Összeállítjuk a szép, rangsorolt HTML e-mail tartalmát
        html_content = f"""
        <div style="font-family: Arial, sans-serif; color: #333; max-width: 600px; margin: 0 auto;">
            <h2 style="color: #2563eb; border-bottom: 2px solid #2563eb; padding-bottom: 10px;">HireWise AI – Heti Jelölt Riport</h2>
            <p>Itt vannak a legújabb jelentkezők a(z) <b style="color: #2563eb;">{link.profession}</b> pozícióra, alkalmassági sorrendben:</p>
            
            <table border="0" cellpadding="10" cellspacing="0" style="width: 100%; border-collapse: collapse; margin-top: 20px; box-shadow: 0 2px 5px rgba(0,0,0,0.05);">
                <tr style="background-color: #2563eb; color: white; text-align: left;">
                    <th style="border-top-left-radius: 4px; border-bottom-left-radius: 4px;">Psz.</th>
                    <th>Név / Elérhetőség</th>
                    <th style="border-top-right-radius: 4px; border-bottom-right-radius: 4px;">AI Értékelés / Indoklás</th>
                </tr>
        """
        
        # Ha az AI-tól kaptunk sikeres eredményt, abból építjük fel a táblázatot
        if results:
            for res in results:
                nev = res.get("nev", "Ismeretlen")
                pontszam = res.get("pontszam", 0)
                indoklas = res.get("indoklas", "")
                
                # Megkeressük az eredeti adatokat (telefonszám, email) a név alapján
                orig_app = next((x for x in apps_list if x["name"].lower() == nev.lower()), {})
                tel = orig_app.get("phone", "-")
                email = orig_app.get("email", "-")
                
                # Pontszám színe (zöld ha jó, piros ha gyenge)
                score_color = "#10b981" if pontszam >= 7 else ("#f59e0b" if pontszam >= 5 else "#ef4444")
                
                html_content += f"""
                <tr style="border-bottom: 1px solid #e5e7eb;">
                    <td style="font-size: 20px; font-weight: bold; color: {score_color}; text-align: center; width: 50px;">
                        {pontszam}
                    </td>
                    <td style="font-size: 14px; width: 180px;">
                        <b>{nev}</b><br>
                        <span style="color: #6b7280; font-size: 12px;">{email}<br>{tel}</span>
                    </td>
                    <td style="font-size: 13px; color: #4b5563; line-height: 1.4;">
                        {indoklas}
                    </td>
                </tr>
                """
        else:
            # Biztonsági játék: ha az AI épp nem elérhető, a sima listát küldjük ki pontok nélkül
            for a in apps_list:
                html_content += f"""
                <tr style="border-bottom: 1px solid #e5e7eb;">
                    <td style="text-align: center; color: #9ca3af;">-</td>
                    <td style="font-size: 14px;"><b>{a['name']}</b><br><span style="color: #6b7280; font-size: 12px;">{a['email']}</span></td>
                    <td style="font-size: 13px; color: #9ca3af;">Az AI értékelés jelenleg nem elérhető.</td>
                </tr>
                """
            
        html_content += """
            </table>
            <p style="margin-top: 30px; font-size: 12px; color: #9ca3af; text-align: center; border-top: 1px solid #e5e7eb; padding-top: 15px;">
                Ezt a levelet a HireWise AI asszisztense generálta automatikusan.
            </p>
        </div>
        """
        
        try:
            # E-mail kiküldése a Resend API-val
            resend.Emails.send({
                "from": "onboarding@resend.dev",
                "to": link.company_email,  # Teszt fázisban ez a Te e-mail címed lesz
                "subject": f"🔥 AI Rangsorolt Jelölt Riport: {link.profession}",
                "html": html_content
            })
            sent_count += 1
        except Exception as e:
            print(f"Hiba történt a {link.company_email} címre küldéskor: {e}")
            
    db.close()
    return {"ok": True, "sent_emails": sent_count}
