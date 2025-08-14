from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from datetime import datetime, timedelta
from io import BytesIO
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet

app = FastAPI()

# Egyszerű adatbázis (memóriában tárolva)
database = {}

# 1. LINK GENERÁLÁS
@app.post("/generate-link")
def generate_link(client_id: str, profession: str, email: str):
    form_url = f"https://example.com/form/{client_id}"
    expires = datetime.now() + timedelta(days=30)

    database[client_id] = {
        "name": "",
        "email": email,
        "profession": profession,
        "form_url": form_url,
        "expires": expires.isoformat(),
        "answers": []
    }

    return {
        "form_url": form_url,
        "expires": expires.isoformat()
    }

# 2. ŰRLAP ÉRTÉKELÉS (mock AI értékelés)
@app.post("/evaluate")
def evaluate(client_id: str, name: str, answers: list):
    if client_id not in database:
        raise HTTPException(status_code=404, detail="Client ID not found")

    database[client_id]["name"] = name

    evaluated_answers = []
    for ans in answers:
        evaluated_answers.append({
            "question": ans["question"],
            "answer": ans["answer"],
            "evaluation": "Megfelelő" if len(ans["answer"]) > 5 else "Rövid"
        })

    database[client_id]["answers"] = evaluated_answers

    return {"status": "ok", "evaluated_count": len(evaluated_answers)}

# 3. PDF RIPORT GENERÁLÁS
@app.get("/report/{client_id}")
def get_report(client_id: str):
    if client_id not in database:
        raise HTTPException(status_code=404, detail="Report not found")

    data = database[client_id]

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4)
    elements = []
    styles = getSampleStyleSheet()

    # Cím
    title = Paragraph(f"Jelentkező értékelése - {data['name']}", styles['Title'])
    elements.append(title)
    elements.append(Spacer(1, 12))

    # Alap adatok
    elements.append(Paragraph(f"<b>Név:</b> {data['name']}", styles['Normal']))
    elements.append(Paragraph(f"<b>E-mail:</b> {data['email']}", styles['Normal']))
    elements.append(Paragraph(f"<b>Szakma:</b> {data['profession']}", styles['Normal']))
    elements.append(Spacer(1, 12))

    # Táblázat
    table_data = [["Kérdés", "Válasz", "AI értékelés"]]
    for ans in data["answers"]:
        table_data.append([
            ans["question"],
            ans["answer"],
            ans.get("evaluation", "N/A")
        ])

    table = Table(table_data, colWidths=[150, 200, 150])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#007BFF")),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 12),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
        ('BACKGROUND', (0, 1), (-1, -1), colors.whitesmoke),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey)
    ]))

    elements.append(table)

    doc.build(elements)
    buffer.seek(0)

    return StreamingResponse(
        buffer,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=report_{client_id}.pdf"}
    )
