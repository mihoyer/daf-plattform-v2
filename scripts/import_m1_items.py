#!/usr/bin/env python3
"""
Import-Skript für M1 Item Bank.

Liest eine JSON-Datei mit Aufgaben ein, validiert sie und importiert sie
in die PostgreSQL-Datenbank (Tabelle m1_items).

Verwendung:
    python scripts/import_m1_items.py aufgaben.json
    python scripts/import_m1_items.py aufgaben.json --dry-run
    python scripts/import_m1_items.py aufgaben.json --deactivate-existing

Erwartetes JSON-Format (Array):
[
  {
    "cefr_level": "A2",
    "category": "Grammatik",
    "topic": "Wechselpräpositionen",
    "context": "Wohnungssuche",
    "sentence": "Wir stellen das Sofa ___ das Fenster.",
    "options": ["an", "auf", "in", "vor"],
    "correct_answer": "vor"
  },
  ...
]
"""
import argparse
import json
import os
import sys
from pathlib import Path

# Projektpfad zum Python-Pfad hinzufügen
sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

# ── Konfiguration ─────────────────────────────────────────────────────────────

GUELTIGE_NIVEAUS = {"A1", "A2", "B1", "B2", "C1", "C2"}
GUELTIGE_KATEGORIEN = {"Grammatik", "Wortschatz"}
PFLICHTFELDER = {"cefr_level", "category", "topic", "context", "sentence", "options", "correct_answer"}


# ── Validierung ───────────────────────────────────────────────────────────────

def validiere_item(item: dict, index: int) -> list[str]:
    """Gibt eine Liste von Fehlermeldungen zurück (leer = gültig)."""
    fehler = []

    # Pflichtfelder
    fehlende = PFLICHTFELDER - set(item.keys())
    if fehlende:
        fehler.append(f"  Fehlende Felder: {', '.join(sorted(fehlende))}")
        return fehler  # Weitere Prüfungen sinnlos

    # CEFR-Niveau
    if item["cefr_level"] not in GUELTIGE_NIVEAUS:
        fehler.append(f"  Ungültiges cefr_level: '{item['cefr_level']}' (erwartet: {GUELTIGE_NIVEAUS})")

    # Kategorie
    if item["category"] not in GUELTIGE_KATEGORIEN:
        fehler.append(f"  Ungültige category: '{item['category']}' (erwartet: {GUELTIGE_KATEGORIEN})")

    # Lücke im Satz
    if "___" not in item["sentence"]:
        fehler.append(f"  Kein '___' im sentence: '{item['sentence'][:80]}'")

    # Optionen
    if not isinstance(item["options"], list):
        fehler.append("  'options' muss ein Array sein")
    elif len(item["options"]) != 4:
        fehler.append(f"  'options' muss genau 4 Einträge haben (gefunden: {len(item['options'])})")
    else:
        # Korrekte Antwort muss in Optionen enthalten sein
        if item["correct_answer"] not in item["options"]:
            fehler.append(
                f"  correct_answer '{item['correct_answer']}' nicht in options {item['options']}"
            )
        # Duplikate in Optionen
        if len(set(item["options"])) != len(item["options"]):
            fehler.append(f"  Doppelte Einträge in options: {item['options']}")

    # Leere Strings
    for feld in ("topic", "context", "sentence", "correct_answer"):
        if not str(item.get(feld, "")).strip():
            fehler.append(f"  Feld '{feld}' ist leer")

    return fehler


# ── Hauptlogik ────────────────────────────────────────────────────────────────

async def importiere(
    json_pfad: str,
    dry_run: bool = False,
    deactivate_existing: bool = False,
):
    # JSON laden
    try:
        with open(json_pfad, "r", encoding="utf-8") as f:
            daten = json.load(f)
    except FileNotFoundError:
        print(f"❌ Datei nicht gefunden: {json_pfad}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"❌ JSON-Fehler: {e}")
        sys.exit(1)

    if not isinstance(daten, list):
        print("❌ JSON muss ein Array (Liste) sein.")
        sys.exit(1)

    print(f"📂 {len(daten)} Einträge in '{json_pfad}' gefunden.\n")

    # Validierung
    gueltig = []
    fehler_gesamt = 0
    for i, item in enumerate(daten):
        fehler = validiere_item(item, i)
        if fehler:
            print(f"⚠️  Eintrag #{i+1} ungültig:")
            for f in fehler:
                print(f)
            fehler_gesamt += 1
        else:
            gueltig.append(item)

    print(f"\n✅ {len(gueltig)} gültige Einträge | ❌ {fehler_gesamt} ungültige Einträge\n")

    if not gueltig:
        print("Keine gültigen Einträge zum Importieren. Abbruch.")
        sys.exit(1)

    if dry_run:
        print("🔍 DRY RUN – keine Datenbankänderungen.")
        # Statistik ausgeben
        from collections import Counter
        niveau_zähler = Counter(i["cefr_level"] for i in gueltig)
        kat_zähler = Counter(i["category"] for i in gueltig)
        print("\nStatistik der gültigen Einträge:")
        print(f"  Nach Niveau: {dict(sorted(niveau_zähler.items()))}")
        print(f"  Nach Kategorie: {dict(sorted(kat_zähler.items()))}")
        return

    # Datenbankverbindung
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        # .env-Datei versuchen
        env_pfad = Path(__file__).parent.parent / ".env"
        if env_pfad.exists():
            for zeile in env_pfad.read_text().splitlines():
                if zeile.startswith("DATABASE_URL="):
                    db_url = zeile.split("=", 1)[1].strip().strip('"').strip("'")
                    break

    if not db_url:
        print("❌ DATABASE_URL nicht gesetzt. Bitte als Umgebungsvariable setzen.")
        sys.exit(1)

    # asyncpg-URL sicherstellen
    if db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    elif db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql+asyncpg://", 1)

    engine = create_async_engine(db_url, echo=False)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    # Import
    from app.models.database import M1Item, Base

    async with engine.begin() as conn:
        # Tabelle anlegen falls nicht vorhanden
        await conn.run_sync(Base.metadata.create_all)

    async with SessionLocal() as session:
        if deactivate_existing:
            from sqlalchemy import update
            await session.execute(update(M1Item).values(is_active=False))
            await session.commit()
            print("🔕 Alle bestehenden Items deaktiviert.\n")

        importiert = 0
        übersprungen = 0

        for item in gueltig:
            # Duplikat-Check: gleicher Satz bereits vorhanden?
            result = await session.execute(
                select(M1Item).where(M1Item.sentence == item["sentence"])
            )
            vorhandenes = result.scalar_one_or_none()

            if vorhandenes:
                print(f"⏭️  Übersprungen (Duplikat): '{item['sentence'][:60]}...'")
                übersprungen += 1
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
            session.add(neues_item)
            importiert += 1

        await session.commit()

    print(f"\n🎉 Import abgeschlossen:")
    print(f"   ✅ {importiert} neue Items importiert")
    print(f"   ⏭️  {übersprungen} Duplikate übersprungen")

    await engine.dispose()


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="M1 Item Bank Import")
    parser.add_argument("json_datei", help="Pfad zur JSON-Datei mit den Aufgaben")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Nur validieren, nichts in die Datenbank schreiben",
    )
    parser.add_argument(
        "--deactivate-existing",
        action="store_true",
        help="Alle bestehenden Items vor dem Import deaktivieren",
    )
    args = parser.parse_args()

    asyncio.run(importiere(args.json_datei, args.dry_run, args.deactivate_existing))
