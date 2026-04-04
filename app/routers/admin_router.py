"""Admin-Router: Passwortgeschütztes Dashboard für Lehrkräfte."""
import hashlib
import hmac
import io
import json
import random
import string
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models.database import TestSession, ModulErgebnis, ModulStatus, SessionStatus, GutscheinCode, PaketTyp, M1Item, get_db
from app.services.session_service import lade_session

router = APIRouter(prefix="/api/admin")


def _erstelle_token(passwort: str) -> str:
    """Deterministischer Token aus Passwort + Secret (kein Worker-State nötig)."""
    return hmac.new(
        settings.secret_key.encode(),
        passwort.encode(),
        hashlib.sha256
    ).hexdigest()


def _prüfe_admin(request: Request):
    token = request.cookies.get("admin_token") or request.headers.get("X-Admin-Token")
    erwartet = _erstelle_token(settings.admin_password)
    if not token or not hmac.compare_digest(token, erwartet):
        raise HTTPException(status_code=401, detail="Nicht autorisiert.")


@router.post("/login")
async def admin_login(request: Request):
    body = await request.json()
    passwort = body.get("passwort", "")
    if passwort != settings.admin_password:
        raise HTTPException(status_code=401, detail="Falsches Passwort.")
    token = _erstelle_token(settings.admin_password)
    response = JSONResponse({"status": "ok"})
    response.set_cookie("admin_token", token, httponly=True, samesite="strict", max_age=3600 * 8)
    return response


@router.post("/logout")
async def admin_logout():
    response = JSONResponse({"status": "ok"})
    response.delete_cookie("admin_token")
    return response


@router.get("/sessions")
async def liste_sessions(
    request: Request,
    seite: int = 1,
    limit: int = 20,
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    _prüfe_admin(request)
    offset = (seite - 1) * limit

    query = select(TestSession).options(selectinload(TestSession.module)).order_by(desc(TestSession.erstellt_am))
    if status:
        query = query.where(TestSession.status == status)

    result = await db.execute(query.offset(offset).limit(limit))
    sessions = result.scalars().all()

    count_result = await db.execute(select(func.count(TestSession.id)))
    total = count_result.scalar()

    return {
        "sessions": [_session_summary(s) for s in sessions],
        "total": total,
        "seite": seite,
        "seiten": (total + limit - 1) // limit,
    }


@router.get("/session/{token}")
async def session_detail(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    _prüfe_admin(request)
    sess = await lade_session(db, token)
    if not sess:
        raise HTTPException(status_code=404, detail="Session nicht gefunden.")

    module_data = []
    for m in sorted(sess.module, key=lambda x: x.reihenfolge):
        module_data.append({
            "modul": m.modul.value,
            "status": m.status.value,
            "cefr": m.cefr_niveau.value if m.cefr_niveau else None,
            "score": m.gesamt_score,
            "analyse": m.get_ki_analyse(),
            "schwierigkeitsgrad": m.schwierigkeitsgrad,
        })

    return {
        **_session_summary(sess),
        "module": module_data,
    }


@router.get("/statistik")
async def statistik(request: Request, db: AsyncSession = Depends(get_db)):
    _prüfe_admin(request)

    total_result = await db.execute(select(func.count(TestSession.id)))
    total = total_result.scalar()

    abgeschlossen_result = await db.execute(
        select(func.count(TestSession.id)).where(TestSession.status == SessionStatus.abgeschlossen)
    )
    abgeschlossen = abgeschlossen_result.scalar()

    avg_result = await db.execute(
        select(func.avg(TestSession.gesamt_score)).where(TestSession.gesamt_score.isnot(None))
    )
    avg_score = avg_result.scalar()

    return {
        "gesamt_sessions": total,
        "abgeschlossene_sessions": abgeschlossen,
        "durchschnitt_score": round(float(avg_score), 1) if avg_score else None,
    }


@router.delete("/session/{token}")
async def loesche_session(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    _prüfe_admin(request)
    sess = await lade_session(db, token)
    if not sess:
        raise HTTPException(status_code=404, detail="Session nicht gefunden.")
    await db.delete(sess)
    await db.commit()
    return {"status": "gelöscht"}


@router.get("/codes")
async def liste_codes(request: Request, db: AsyncSession = Depends(get_db)):
    """Alle Gutscheincodes auflisten."""
    _prüfe_admin(request)
    result = await db.execute(select(GutscheinCode).order_by(desc(GutscheinCode.erstellt_am)))
    codes = result.scalars().all()
    return {"codes": [
        {
            "id": c.id,
            "code": c.code,
            "paket": c.paket.value,
            "max_nutzungen": c.max_nutzungen,
            "genutzt": c.genutzt,
            "aktiv": c.aktiv,
            "notiz": c.notiz,
            "erstellt_am": c.erstellt_am.strftime("%d.%m.%Y") if c.erstellt_am else "–",
            "laeuft_ab_am": c.laeuft_ab_am.strftime("%d.%m.%Y") if c.laeuft_ab_am else "–",
        }
        for c in codes
    ]}


@router.post("/codes/erstelle")
async def erstelle_codes(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Neue Gutscheincodes erstellen."""
    _prüfe_admin(request)
    body = await request.json()
    anzahl = int(body.get("anzahl", 1))
    paket = body.get("paket", "premium")
    max_nutzungen = int(body.get("max_nutzungen", 1))
    notiz = body.get("notiz", "")
    prefix = body.get("prefix", "TG").upper().strip()

    if anzahl < 1 or anzahl > 100:
        raise HTTPException(status_code=400, detail="Anzahl muss zwischen 1 und 100 liegen.")

    try:
        paket_enum = PaketTyp(paket)
    except ValueError:
        paket_enum = PaketTyp.premium

    neue_codes = []
    for _ in range(anzahl):
        # Eindeutigen Code generieren
        for attempt in range(20):
            suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
            code = f"{prefix}-{suffix}" if prefix else suffix
            existing = await db.execute(select(GutscheinCode).where(GutscheinCode.code == code))
            if not existing.scalar_one_or_none():
                break
        gc = GutscheinCode(
            code=code,
            paket=paket_enum,
            max_nutzungen=max_nutzungen,
            genutzt=0,
            aktiv=True,
            notiz=notiz if notiz else None,
        )
        db.add(gc)
        neue_codes.append(code)

    await db.commit()
    return {"codes": neue_codes, "anzahl": len(neue_codes)}


@router.post("/codes/{code_id}/deaktiviere")
async def deaktiviere_code(code_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Gutscheincode deaktivieren."""
    _prüfe_admin(request)
    result = await db.execute(select(GutscheinCode).where(GutscheinCode.id == code_id))
    gc = result.scalar_one_or_none()
    if not gc:
        raise HTTPException(status_code=404, detail="Code nicht gefunden.")
    gc.aktiv = False
    await db.commit()
    return {"status": "deaktiviert", "code": gc.code}


@router.delete("/codes/{code_id}")
async def loesche_code(code_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Einzelnen Gutscheincode löschen (nur deaktivierte oder vollständig verbrauchte)."""
    _prüfe_admin(request)
    result = await db.execute(select(GutscheinCode).where(GutscheinCode.id == code_id))
    gc = result.scalar_one_or_none()
    if not gc:
        raise HTTPException(status_code=404, detail="Code nicht gefunden.")
    if gc.aktiv and gc.genutzt < gc.max_nutzungen:
        raise HTTPException(status_code=400, detail="Aktive Codes können nicht gelöscht werden. Bitte zuerst deaktivieren.")
    await db.delete(gc)
    await db.commit()
    return {"status": "gelöscht", "code": gc.code}


@router.delete("/codes")
async def loesche_inaktive_codes(request: Request, db: AsyncSession = Depends(get_db)):
    """Alle deaktivierten und vollständig verbrauchten Codes löschen."""
    _prüfe_admin(request)
    from sqlalchemy import or_, and_
    result = await db.execute(
        select(GutscheinCode).where(
            or_(
                GutscheinCode.aktiv == False,
                and_(GutscheinCode.aktiv == True, GutscheinCode.genutzt >= GutscheinCode.max_nutzungen)
            )
        )
    )
    codes = result.scalars().all()
    anzahl = len(codes)
    for gc in codes:
        await db.delete(gc)
    await db.commit()
    return {"status": "gelöscht", "anzahl": anzahl}


@router.get("/codes/qr-pdf")
async def codes_qr_pdf(
    request: Request,
    db: AsyncSession = Depends(get_db),
    prefix: str = "TG",
    nur_aktive: bool = True,
):
    """QR-Code-PDF für alle aktiven Gutscheincodes generieren."""
    _prüfe_admin(request)

    query = select(GutscheinCode).order_by(GutscheinCode.erstellt_am.asc())
    if nur_aktive:
        query = query.where(GutscheinCode.aktiv == True)
    result = await db.execute(query)
    codes = result.scalars().all()

    if not codes:
        raise HTTPException(status_code=404, detail="Keine aktiven Codes vorhanden.")

    pdf_bytes = _generiere_qr_pdf(codes)
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=testgruppe_qrcodes.pdf"},
    )


def _generiere_qr_pdf(codes: list) -> bytes:
    """QR-Code-PDF mit je 4 Codes pro Seite generieren."""
    import qrcode
    import tempfile
    import os
    import matplotlib
    matplotlib.use("Agg")
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image as RLImage, PageBreak
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.enums import TA_CENTER

    BASE_URL = settings.kandidat_base_url.rstrip('/') + "/k/"

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=1.5*cm, rightMargin=1.5*cm,
        topMargin=2*cm, bottomMargin=2*cm,
    )

    style_titel = ParagraphStyle("titel", fontName="Helvetica-Bold", fontSize=11, alignment=TA_CENTER, spaceAfter=4)
    style_code = ParagraphStyle("code", fontName="Helvetica-Bold", fontSize=14, alignment=TA_CENTER, textColor=colors.HexColor("#1a56db"), spaceAfter=2)
    style_url = ParagraphStyle("url", fontName="Helvetica", fontSize=7, alignment=TA_CENTER, textColor=colors.HexColor("#6b7280"), spaceAfter=2)
    style_info = ParagraphStyle("info", fontName="Helvetica", fontSize=8, alignment=TA_CENTER, textColor=colors.HexColor("#374151"))
    style_header = ParagraphStyle("header", fontName="Helvetica-Bold", fontSize=16, alignment=TA_CENTER, spaceAfter=6)
    style_sub = ParagraphStyle("sub", fontName="Helvetica", fontSize=10, alignment=TA_CENTER, textColor=colors.HexColor("#6b7280"), spaceAfter=20)

    story = []

    # Deckblatt-Header
    story.append(Spacer(1, 1*cm))
    story.append(Paragraph("DaF/DaZ Sprachdiagnostik", style_header))
    story.append(Paragraph(f"Zugangscodes für Testgruppe – {len(codes)} Teilnehmende", style_sub))

    tmpfiles = []
    # Je 4 Codes pro Zeile, 2 Zeilen pro Seite = 8 pro Seite
    COLS = 2
    ROWS_PER_PAGE = 4
    PER_PAGE = COLS * ROWS_PER_PAGE

    for page_start in range(0, len(codes), PER_PAGE):
        page_codes = codes[page_start:page_start + PER_PAGE]
        # Auffüllen auf volle Seite
        while len(page_codes) % COLS != 0:
            page_codes.append(None)

        table_data = []
        for row_start in range(0, len(page_codes), COLS):
            row_codes = page_codes[row_start:row_start + COLS]
            cell_row = []
            for gc in row_codes:
                if gc is None:
                    cell_row.append("")
                    continue
                url = f"{BASE_URL}{gc.code}"
                # QR-Code generieren
                qr = qrcode.QRCode(version=1, box_size=6, border=2)
                qr.add_data(url)
                qr.make(fit=True)
                qr_img = qr.make_image(fill_color="black", back_color="white")
                tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                qr_img.save(tmp.name)
                tmpfiles.append(tmp.name)

                cell_content = [
                    Paragraph("DaF/DaZ Sprachdiagnostik", style_titel),
                    RLImage(tmp.name, width=4.5*cm, height=4.5*cm),
                    Paragraph(gc.code, style_code),
                    Paragraph(url, style_url),
                    Paragraph(f"Paket: {gc.paket.value.capitalize()} | Nutzungen: {gc.max_nutzungen}", style_info),
                    Paragraph(gc.notiz or "", ParagraphStyle("notiz", fontName="Helvetica-Oblique", fontSize=7, alignment=TA_CENTER, textColor=colors.HexColor("#9ca3af"))),
                ]
                cell_row.append(cell_content)
            table_data.append(cell_row)

        col_width = (A4[0] - 3*cm) / COLS
        t = Table(table_data, colWidths=[col_width]*COLS, rowHeights=[8*cm]*len(table_data))
        t.setStyle(TableStyle([
            ("BOX", (0,0), (-1,-1), 0.5, colors.HexColor("#e5e7eb")),
            ("INNERGRID", (0,0), (-1,-1), 0.5, colors.HexColor("#e5e7eb")),
            ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
            ("ALIGN", (0,0), (-1,-1), "CENTER"),
            ("BACKGROUND", (0,0), (-1,-1), colors.HexColor("#f9fafb")),
            ("ROWBACKGROUNDS", (0,0), (-1,-1), [colors.white, colors.HexColor("#f3f4f6")]),
        ]))
        story.append(t)
        if page_start + PER_PAGE < len(codes):
            story.append(PageBreak())

    # Footer-Hinweis
    story.append(Spacer(1, 1*cm))
    story.append(Paragraph(
        "Konzept, Didaktik & Entwicklung © Iryna Hoyer | DaF/DaZ Sprachdiagnostik",
        ParagraphStyle("footer", fontName="Helvetica", fontSize=8, alignment=TA_CENTER, textColor=colors.HexColor("#9ca3af"))
    ))

    doc.build(story)

    # Temp-Dateien aufräumen
    for f in tmpfiles:
        try:
            os.unlink(f)
        except Exception:
            pass

    return buf.getvalue()


def _session_summary(sess: TestSession) -> dict:
    return {
        "token": sess.token,
        "paket": sess.paket.value,
        "status": sess.status.value,
        "zahlungs_status": sess.zahlungs_status.value,
        "gesamt_score": sess.gesamt_score,
        "gesamt_niveau": sess.gesamt_niveau.value if sess.gesamt_niveau else None,
        "grob_niveau": sess.grob_niveau.value if sess.grob_niveau else None,
        "hilfssprache": sess.hilfssprache.value,
        "erstellt_am": sess.erstellt_am.isoformat(),
        "abgeschlossen_am": sess.abgeschlossen_am.isoformat() if sess.abgeschlossen_am else None,
        "modul_count": len(sess.module),
        "abgeschlossene_module": sum(1 for m in sess.module if m.status == ModulStatus.abgeschlossen),
    }


# ── Item Bank (M1) ────────────────────────────────────────────────────────────

@router.get("/item-bank")
async def liste_items(
    request: Request,
    seite: int = 1,
    limit: int = 50,
    niveau: Optional[str] = None,
    kategorie: Optional[str] = None,
    nur_aktive: bool = False,
    db: AsyncSession = Depends(get_db),
):
    """Listet alle Items der M1 Item Bank mit Filteroptionen."""
    _prüfe_admin(request)
    offset = (seite - 1) * limit

    from sqlalchemy import and_
    bedingungen = []
    if niveau:
        bedingungen.append(M1Item.cefr_level == niveau)
    if kategorie:
        bedingungen.append(M1Item.category == kategorie)
    if nur_aktive:
        bedingungen.append(M1Item.is_active == True)

    query = select(M1Item).order_by(M1Item.cefr_level, M1Item.id)
    if bedingungen:
        query = query.where(and_(*bedingungen))

    count_query = select(func.count(M1Item.id))
    if bedingungen:
        count_query = count_query.where(and_(*bedingungen))

    result = await db.execute(query.offset(offset).limit(limit))
    items = result.scalars().all()

    count_result = await db.execute(count_query)
    total = count_result.scalar()

    # Statistik nach Niveau
    stats_result = await db.execute(
        select(M1Item.cefr_level, M1Item.category, func.count(M1Item.id))
        .where(M1Item.is_active == True)
        .group_by(M1Item.cefr_level, M1Item.category)
    )
    stats_rows = stats_result.all()
    statistik = {}
    for row in stats_rows:
        niv, kat, count = row
        if niv not in statistik:
            statistik[niv] = {"Grammatik": 0, "Wortschatz": 0, "gesamt": 0}
        statistik[niv][kat] = statistik[niv].get(kat, 0) + count
        statistik[niv]["gesamt"] += count

    return {
        "items": [item.to_dict() for item in items],
        "total": total,
        "seite": seite,
        "limit": limit,
        "statistik": statistik,
    }


@router.patch("/item-bank/{item_id}/toggle")
async def toggle_item(
    item_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Aktiviert oder deaktiviert ein einzelnes Item."""
    _prüfe_admin(request)
    result = await db.execute(select(M1Item).where(M1Item.id == item_id))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Item nicht gefunden.")
    item.is_active = not item.is_active
    await db.commit()
    return {"id": item.id, "is_active": item.is_active}


@router.delete("/item-bank/{item_id}")
async def loesche_item(
    item_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Löscht ein Item dauerhaft aus der Item Bank."""
    _prüfe_admin(request)
    result = await db.execute(select(M1Item).where(M1Item.id == item_id))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Item nicht gefunden.")
    await db.delete(item)
    await db.commit()
    return {"status": "geloescht", "id": item_id}


@router.post("/item-bank/import")
async def importiere_items(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Importiert Items aus einem JSON-Array im Request-Body.
    Body: {"items": [...], "deactivate_existing": false}
    """
    _prüfe_admin(request)
    body = await request.json()
    items_data = body.get("items", [])
    deactivate_existing = body.get("deactivate_existing", False)

    if not isinstance(items_data, list):
        raise HTTPException(status_code=400, detail="'items' muss ein Array sein.")

    GUELTIGE_NIVEAUS = {"A1", "A2", "B1", "B2", "C1", "C2"}
    GUELTIGE_KATEGORIEN = {"Grammatik", "Wortschatz"}

    if deactivate_existing:
        from sqlalchemy import update
        await db.execute(update(M1Item).values(is_active=False))
        await db.commit()

    importiert = 0
    fehler = []
    duplikate = 0

    for i, item in enumerate(items_data):
        # Validierung
        pflichtfelder = {"cefr_level", "category", "topic", "context", "sentence", "options", "correct_answer"}
        fehlende = pflichtfelder - set(item.keys())
        if fehlende:
            fehler.append(f"Eintrag #{i+1}: Fehlende Felder: {fehlende}")
            continue
        if item["cefr_level"] not in GUELTIGE_NIVEAUS:
            fehler.append(f"Eintrag #{i+1}: Ungültiges cefr_level '{item['cefr_level']}'")
            continue
        if item["category"] not in GUELTIGE_KATEGORIEN:
            fehler.append(f"Eintrag #{i+1}: Ungültige category '{item['category']}'")
            continue
        if "___" not in item["sentence"]:
            fehler.append(f"Eintrag #{i+1}: Kein '___' im sentence")
            continue
        if not isinstance(item["options"], list) or len(item["options"]) != 4:
            fehler.append(f"Eintrag #{i+1}: 'options' muss genau 4 Einträge haben")
            continue
        if item["correct_answer"] not in item["options"]:
            fehler.append(f"Eintrag #{i+1}: correct_answer nicht in options")
            continue

        # Duplikat-Check
        from sqlalchemy import select as sa_select
        dup_result = await db.execute(sa_select(M1Item).where(M1Item.sentence == item["sentence"]))
        if dup_result.scalar_one_or_none():
            duplikate += 1
            continue

        neues_item = M1Item(
            cefr_level=item["cefr_level"],
            category=item["category"],
            topic=item["topic"],
            context=item["context"],
            sentence=item["sentence"],
            options=item["options"],
            correct_answer=item["correct_answer"],
            is_active=True,
        )
        db.add(neues_item)
        importiert += 1

    await db.commit()

    return {
        "importiert": importiert,
        "duplikate": duplikate,
        "fehler_count": len(fehler),
        "fehler": fehler[:20],  # Max 20 Fehlermeldungen zurückgeben
    }
