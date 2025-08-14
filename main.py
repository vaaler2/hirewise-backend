from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, JSONResponse
import uuid

app = FastAPI()

# In-memory storage a generált linkekhez
generated_links = {}

@app.post("/generate-link")
async def generate_link(client_id: str = Form(...), profession: str = Form(...)):
    link_id = str(uuid.uuid4())
    form_url = f"https://example.com/form/{client_id}"
    generated_links[link_id] = {"client_id": client_id, "profession": profession, "form_url": form_url}
    return JSONResponse(content={"form_url": form_url})


@app.get("/generate-report", response_class=HTMLResponse)
async def generate_report():
    # Tesztadatok
    applicants = [
        {"name": "Kiss Péter", "score": 92, "comment": "Kiemelkedő szakmai tapasztalat"},
        {"name": "Nagy Anna", "score": 85, "comment": "Erős kommunikációs készségek"},
        {"name": "Szabó Gábor", "score": 78, "comment": "Jó problémamegoldó képesség"},
        {"name": "Tóth Éva", "score": 88, "comment": "Gyorsan tanul, kreatív"},
        {"name": "Horváth László", "score": 81, "comment": "Megbízható, precíz"}
    ]

    # HTML táblázat sablon
    html_content = """
    <html>
    <head>
        <style>
            body { font-family: Arial, sans-serif; margin: 40px; background-color: #f9f9f9; }
            h2 { color: #333; }
            table { border-collapse: collapse; width: 100%; background: white; }
            th, td { border: 1px solid #ddd; padding: 12px; text-align: left; }
            th { background-color: #007BFF; color: white; }
            tr:nth-child(even) { background-color: #f2f2f2; }
        </style>
    </head>
    <body>
        <h2>Jelentkezők értékelése</h2>
        <table>
            <tr>
                <th>Név</th>
                <th>Pontszám</th>
                <th>Megjegyzés</th>
            </tr>
    """

    for applicant in applicants:
        html_content += f"""
            <tr>
                <td>{applicant['name']}</td>
                <td>{applicant['score']}</td>
                <td>{applicant['comment']}</td>
            </tr>
        """

    html_content += """
        </table>
    </body>
    </html>
    """

    return HTMLResponse(content=html_content)
