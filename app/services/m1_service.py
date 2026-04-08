"""
M1-Service: Grammatik & Wortschatz – Statische Item Bank (Lückentext-Modus).

Ablauf:
1. starte_adaptiven_test()  → 3 Einstiegsfragen aus DB (A2 / B1 / B2 gemischt)
2. naechste_frage()         → nach jeder Antwort Niveau neu schätzen, nächste Frage aus DB
3. werte_aus()              → Endauswertung (Score, CEFR, Details)

Keine KI-Aufrufe mehr für Fragen-Generierung.
Adaptivität läuft vollständig im Python-Algorithmus.
Auswertung: Texteingabe des Lernenden wird direkt mit correct_answer verglichen
(case-insensitive, Umlaute normalisiert).
"""
import json
import random
import unicodedata
from typing import Optional

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import M1Item

# ── Konstanten ────────────────────────────────────────────────────────────────

NIVEAUS = ["A1", "A2", "B1", "B2", "C1", "C2"]

# Einstiegs-Niveaus: breite Streuung für schnelle Einschätzung
EINSTIEGS_NIVEAUS = ["A2", "B1", "B2"]

# Grammatik/Wortschatz-Verhältnis: 70% / 30%
KATEGORIE_GEWICHTE = {"Grammatik": 0.70, "Wortschatz": 0.30}


# ── Textvergleich ─────────────────────────────────────────────────────────────

def normalisiere(text: str) -> str:
    """
    Normalisiert eine Texteingabe für den Vergleich:
    - Kleinbuchstaben
    - Führende/nachfolgende Leerzeichen entfernen
    - Umlaute beibehalten (ä, ö, ü, ß sind korrekt)
    - Mehrfache Leerzeichen auf eines reduzieren
    """
    return " ".join(text.strip().lower().split())


def ist_korrekt(eingabe: str, korrekte_antwort: str) -> bool:
    """Vergleicht die Eingabe des Lernenden mit der korrekten Antwort."""
    return normalisiere(eingabe) == normalisiere(korrekte_antwort)


# ── Niveau-Schätzung (IRT-vereinfacht) ───────────────────────────────────────

def schaetze_niveau(antworten_verlauf: list[dict]) -> str:
    """
    Schätzt das aktuelle Niveau basierend auf dem bisherigen Antwortverlauf.
    antworten_verlauf: [{"niveau": "B1", "korrekt": True}, ...]
    """
    if not antworten_verlauf:
        return "B1"

    niveau_punkte = 0.0
    gewicht_gesamt = 0.0

    for i, eintrag in enumerate(antworten_verlauf):
        gewicht = 1.0 + i * 0.3
        niveau_idx = NIVEAUS.index(eintrag["niveau"])

        if eintrag["korrekt"]:
            ziel_idx = min(niveau_idx + 1, len(NIVEAUS) - 1)
        else:
            ziel_idx = max(niveau_idx - 1, 0)

        niveau_punkte += ziel_idx * gewicht
        gewicht_gesamt += gewicht

    geschaetzter_idx = round(niveau_punkte / gewicht_gesamt)
    geschaetzter_idx = max(0, min(geschaetzter_idx, len(NIVEAUS) - 1))
    return NIVEAUS[geschaetzter_idx]


# ── Item-Bank-Abfrage ─────────────────────────────────────────────────────────

async def hole_item_aus_db(
    db: AsyncSession,
    niveau: str,
    bereits_verwendet_ids: list[int],
    bevorzugte_kategorie: Optional[str] = None,
) -> Optional[dict]:
    """
    Holt eine zufällige aktive Aufgabe aus der Item Bank.

    Priorität:
    1. Gewünschtes Niveau + bevorzugte Kategorie
    2. Gewünschtes Niveau (beliebige Kategorie)
    3. Benachbartes Niveau (±1)
    4. Beliebiges aktives Item (Notfall)
    """
    def basis_query(niv: str, kat: Optional[str] = None):
        bedingungen = [
            M1Item.is_active == True,
            M1Item.cefr_level == niv,
        ]
        if bereits_verwendet_ids:
            bedingungen.append(M1Item.id.notin_(bereits_verwendet_ids))
        if kat:
            bedingungen.append(M1Item.category == kat)
        return select(M1Item).where(and_(*bedingungen))

    # Versuch 1: Gewünschtes Niveau + bevorzugte Kategorie
    if bevorzugte_kategorie:
        result = await db.execute(basis_query(niveau, bevorzugte_kategorie))
        kandidaten = result.scalars().all()
        if kandidaten:
            return random.choice(kandidaten).to_dict()

    # Versuch 2: Gewünschtes Niveau, beliebige Kategorie
    result = await db.execute(basis_query(niveau))
    kandidaten = result.scalars().all()
    if kandidaten:
        return random.choice(kandidaten).to_dict()

    # Versuch 3: Benachbarte Niveaus
    idx = NIVEAUS.index(niveau)
    nachbarn = []
    if idx > 0:
        nachbarn.append(NIVEAUS[idx - 1])
    if idx < len(NIVEAUS) - 1:
        nachbarn.append(NIVEAUS[idx + 1])

    for nachbar_niveau in nachbarn:
        result = await db.execute(basis_query(nachbar_niveau))
        kandidaten = result.scalars().all()
        if kandidaten:
            return random.choice(kandidaten).to_dict()

    # Versuch 4: Beliebiges aktives Item (Notfall)
    bedingungen = [M1Item.is_active == True]
    if bereits_verwendet_ids:
        bedingungen.append(M1Item.id.notin_(bereits_verwendet_ids))
    result = await db.execute(select(M1Item).where(and_(*bedingungen)))
    kandidaten = result.scalars().all()
    if kandidaten:
        return random.choice(kandidaten).to_dict()

    return None  # Item Bank leer


def item_zu_frage(item: dict, item_id: int) -> dict:
    """
    Konvertiert ein Item-Bank-Dict in das Frontend-kompatible Fragen-Format.
    Kein options-Mischen mehr – reiner Lückentext-Modus.
    """
    return {
        "id": item_id,
        "frage": item["sentence"].replace("___", "_____"),  # Frontend erwartet 5 Unterstriche
        "korrekte_antwort_text": item["correct_answer"],
        "feedback_text": item.get("feedback_text", ""),
        "niveau": item["cefr_level"],
        "thema": item["topic"],
        # Metadaten für Auswertung
        "item_bank_id": item["id"],
        "category": item["category"],
    }


def bestimme_kategorie(fragen_bisher: list[dict]) -> Optional[str]:
    """
    Bestimmt die bevorzugte Kategorie für die nächste Frage,
    um das 70/30-Verhältnis (Grammatik/Wortschatz) einzuhalten.
    """
    if not fragen_bisher:
        return "Grammatik"

    grammatik_count = sum(1 for f in fragen_bisher if f.get("category") == "Grammatik")
    wortschatz_count = sum(1 for f in fragen_bisher if f.get("category") == "Wortschatz")
    gesamt = grammatik_count + wortschatz_count

    if gesamt == 0:
        return "Grammatik"

    grammatik_anteil = grammatik_count / gesamt
    if grammatik_anteil < KATEGORIE_GEWICHTE["Grammatik"]:
        return "Grammatik"
    else:
        return "Wortschatz"


# ── Öffentliche Service-Funktionen ────────────────────────────────────────────

async def starte_adaptiven_test(db: AsyncSession, hilfssprache: str = "de") -> dict:
    """
    Startet den adaptiven Test aus der Item Bank.
    Gibt 3 Einstiegsfragen (A2, B1, B2 gemischt) zurück.
    """
    einstiegs_niveaus = EINSTIEGS_NIVEAUS.copy()
    random.shuffle(einstiegs_niveaus)

    fragen = []
    verwendet_ids = []
    kategorien_verlauf = []

    for i, niveau in enumerate(einstiegs_niveaus):
        bevorzugte_kat = bestimme_kategorie(kategorien_verlauf)
        item = await hole_item_aus_db(db, niveau, verwendet_ids, bevorzugte_kat)

        if item is None:
            frage = _fallback_frage(niveau, i + 1)
        else:
            frage = item_zu_frage(item, i + 1)
            verwendet_ids.append(item["id"])
            kategorien_verlauf.append({"category": item["category"]})

        fragen.append(frage)

    return {
        "fragen": fragen,
        "geschaetztes_niveau": "B1",
        "antworten_verlauf": [],
        "naechste_id": len(fragen) + 1,
        "gesamt_fragen": 10,
        "phase": "einstieg",
        "verwendet_ids": verwendet_ids,
        "kategorien_verlauf": kategorien_verlauf,
    }


async def naechste_frage(
    db: AsyncSession,
    zustand: dict,
    item_id: int,
    eingabe: str,          # Texteingabe des Lernenden (statt gewaehlt-Index)
    hilfssprache: str = "de",
) -> dict:
    """Verarbeitet eine Antwort und gibt die nächste adaptive Frage aus der Item Bank zurück."""
    antworten_verlauf = zustand.get("antworten_verlauf", [])
    naechste_id = zustand.get("naechste_id", 4)
    alle_fragen = zustand.get("alle_fragen", [])
    verwendet_ids = zustand.get("verwendet_ids", [])
    kategorien_verlauf = zustand.get("kategorien_verlauf", [])

    aktuelle_frage = next(
        (f for f in alle_fragen if f["id"] == item_id), None
    )

    korrekt = False
    fragen_niveau = "B1"
    if aktuelle_frage:
        korrekt = ist_korrekt(eingabe, aktuelle_frage.get("korrekte_antwort_text", ""))
        fragen_niveau = aktuelle_frage.get("niveau", "B1")

    antworten_verlauf.append({
        "item_id": item_id,
        "niveau": fragen_niveau,
        "korrekt": korrekt,
        "eingabe": eingabe,
    })

    neues_niveau = schaetze_niveau(antworten_verlauf)
    bevorzugte_kat = bestimme_kategorie(kategorien_verlauf)

    item = await hole_item_aus_db(db, neues_niveau, verwendet_ids, bevorzugte_kat)

    if item is None:
        neue_frage = _fallback_frage(neues_niveau, naechste_id)
    else:
        neue_frage = item_zu_frage(item, naechste_id)
        verwendet_ids.append(item["id"])
        kategorien_verlauf.append({"category": item["category"]})

    return {
        "frage": neue_frage,
        "geschaetztes_niveau": neues_niveau,
        "antworten_verlauf": antworten_verlauf,
        "naechste_id": naechste_id + 1,
        "korrekt": korrekt,
        "verwendet_ids": verwendet_ids,
        "kategorien_verlauf": kategorien_verlauf,
    }


async def werte_aus(alle_fragen: list[dict], antworten: dict[str, str]) -> dict:
    """
    Endauswertung: berechnet Score, CEFR und detaillierte Analyse.
    antworten: {str(item_id): eingegebener_text}
    """
    korrekt_count = 0
    total = len(alle_fragen)
    details = []
    antworten_verlauf = []

    for frage in alle_fragen:
        item_id = str(frage["id"])
        eingabe = antworten.get(item_id, "")
        korrekte_antwort = frage.get("korrekte_antwort_text", "")
        ist_richtig = ist_korrekt(eingabe, korrekte_antwort)

        if ist_richtig:
            korrekt_count += 1

        antworten_verlauf.append({
            "niveau": frage.get("niveau", "B1"),
            "korrekt": ist_richtig,
        })

        details.append({
            "id": frage["id"],
            "frage": frage["frage"],
            "eingabe": eingabe,
            "korrekte_antwort": korrekte_antwort,
            "ist_korrekt": ist_richtig,
            "feedback_text": frage.get("feedback_text", ""),
            "niveau": frage.get("niveau", "B1"),
            "thema": frage.get("thema", ""),
        })

    prozent = (korrekt_count / total * 100) if total > 0 else 0
    adaptives_niveau = schaetze_niveau(antworten_verlauf)

    if prozent >= 90:
        prozent_cefr = "C1"
    elif prozent >= 75:
        prozent_cefr = "B2"
    elif prozent >= 55:
        prozent_cefr = "B1"
    elif prozent >= 35:
        prozent_cefr = "A2"
    else:
        prozent_cefr = "A1"

    adaptiv_idx = NIVEAUS.index(adaptives_niveau)
    prozent_idx = NIVEAUS.index(prozent_cefr)
    final_idx = round((adaptiv_idx + prozent_idx) / 2)
    final_cefr = NIVEAUS[final_idx]

    score = round(prozent, 1)

    return {
        "score": score,
        "korrekt": korrekt_count,
        "total": total,
        "prozent": prozent,
        "cefr": final_cefr,
        "adaptives_niveau": adaptives_niveau,
        "details": details,
        "staerken": _analysiere_staerken(details),
        "schwaechen": _analysiere_schwaechen(details),
    }


# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

def _analysiere_staerken(details: list[dict]) -> list[str]:
    korrekte_themen = [d["thema"] for d in details if d.get("ist_korrekt") and d.get("thema")]
    return list(dict.fromkeys(korrekte_themen))[:3]


def _analysiere_schwaechen(details: list[dict]) -> list[str]:
    falsche_themen = [d["thema"] for d in details if not d.get("ist_korrekt") and d.get("thema")]
    return list(dict.fromkeys(falsche_themen))[:3]


def _fallback_frage(niveau: str, item_id: int) -> dict:
    """Notfall-Fallback – handverlesene, geprüfte Lückentextfragen pro Niveau."""
    fallbacks = {
        "A1": [
            {"frage": "Ich _____ aus Deutschland.", "korrekte_antwort_text": "komme", "feedback_text": "1. Person Singular Präsens von 'kommen': ich komme.", "thema": "Konjugation Präsens"},
            {"frage": "Das _____ mein Bruder.", "korrekte_antwort_text": "ist", "feedback_text": "3. Person Singular von 'sein': ist.", "thema": "sein/haben"},
        ],
        "A2": [
            {"frage": "Gestern _____ ich ins Kino gegangen.", "korrekte_antwort_text": "bin", "feedback_text": "Perfekt mit 'sein' bei Bewegungsverben (gehen): ich bin gegangen.", "thema": "Perfekt"},
            {"frage": "Kannst du _____ helfen?", "korrekte_antwort_text": "mir", "feedback_text": "Nach 'helfen' steht der Dativ: mir (nicht mich).", "thema": "Dativ"},
        ],
        "B1": [
            {"frage": "Er kommt nicht zur Party, _____ er krank ist.", "korrekte_antwort_text": "weil", "feedback_text": "'weil' leitet einen Kausalsatz ein, das Verb steht am Ende.", "thema": "Kausalsätze"},
            {"frage": "Sie fragt, _____ er morgen Zeit hat.", "korrekte_antwort_text": "ob", "feedback_text": "'ob' leitet indirekte Ja/Nein-Fragen ein.", "thema": "Indirekte Rede"},
        ],
        "B2": [
            {"frage": "Wenn ich mehr Zeit _____, würde ich öfter reisen.", "korrekte_antwort_text": "hätte", "feedback_text": "Konjunktiv II von 'haben': hätte.", "thema": "Konjunktiv II"},
            {"frage": "Das Projekt _____ gestern abgeschlossen.", "korrekte_antwort_text": "wurde", "feedback_text": "Passiv Präteritum: wurde + Partizip II.", "thema": "Passiv"},
        ],
        "C1": [
            {"frage": "_____ seiner Erfahrung konnte er das Problem schnell lösen.", "korrekte_antwort_text": "Aufgrund", "feedback_text": "'Aufgrund' ist eine Präposition mit Genitiv und gibt einen Grund an.", "thema": "Genitiv-Präpositionen"},
        ],
        "C2": [
            {"frage": "Die Entscheidung, _____ er so lange gezögert hatte, fiel ihm schwer.", "korrekte_antwort_text": "über die", "feedback_text": "Relativsatz mit Präposition: 'zögern über' → über die.", "thema": "Relativsätze"},
        ],
    }

    niveau_fallbacks = fallbacks.get(niveau, fallbacks["B1"])
    fb = random.choice(niveau_fallbacks).copy()
    fb["id"] = item_id
    fb["niveau"] = niveau
    fb["category"] = "Grammatik"
    fb["item_bank_id"] = None
    return fb
