"""
Builds a Hebrew Word report for the ML runs that were executed every CHECKPOINT_STEP labeled sessions.

Inputs (produced by analytics_pipeline_final.py --checkpoints and upload_simulation.py):
  simulation_output/analytics_outputs/checkpoint_runs.json
  simulation_output/analytics_outputs/checkpoint_<N>/*.png
  simulation_output/upload_manifest.json   (run_id of every saved run, optional)

Usage:
    py -3 build_checkpoint_report_doc.py [--dir simulation_output]
"""
import os
import json
import shutil
import zipfile
import argparse
import tempfile
from datetime import datetime, timezone

import build_analytics_summary_doc as docx

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
CRITERIA = ["G", "D", "LD", "T", "LW", "C", "A"]
CRITERION_NAMES = {
    "G": "חלונות בין שיעורים",
    "D": "מספר ימי לימוד",
    "LD": "עומס יומי",
    "T": "העדפת שעות",
    "LW": "איזון שבועי",
    "C": "עומס הערכה בקורסים",
    "A": "נוכחות חובה",
}
CHECKPOINT_FIGURES = [
    ("02_model_performance_comparison.png", "השוואת ביצועי המודלים"),
    ("03_criterion_importance_by_model.png", "חשיבות הקריטריונים לפי מודל"),
    ("04_final_optimizer_weights.png", "המשקולות שנבחרו לאופטימייזר"),
    ("05_roc_curve_comparison.png", "עקומות ROC"),
]
LAST_CHECKPOINT_EXTRA = [
    ("00_correlation_matrix.png", "מטריצת קורלציה בין הקריטריונים לבחירת המשתמש"),
    ("01_data_quality_summary.png", "סיכום איכות הנתונים לפי סוג סשן"),
    ("06_confusion_matrix_logistic_regression.png", "מטריצת בלבול - Logistic Regression"),
    ("06_confusion_matrix_random_forest.png", "מטריצת בלבול - Random Forest"),
    ("06_confusion_matrix_xgboost.png", "מטריצת בלבול - XGBoost"),
]


def fmt(value, digits=3):
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "-"


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def run_meta(data_dir, runs):
    """run_id and run time for every checkpoint, when the runs were saved to the DB."""
    manifest_path = os.path.join(data_dir, "upload_manifest.json")
    run_ids = load_json(manifest_path).get("ml_criterion_weights", []) if os.path.exists(manifest_path) else []
    try:
        import upload_simulation
        times = [datetime.fromisoformat(row["created_at"]) for row in upload_simulation.checkpoint_run_rows(data_dir)]
    except Exception:
        times = []
    meta = []
    for i, _ in enumerate(runs):
        meta.append({
            "run_id": str(run_ids[i]) if i < len(run_ids) else "-",
            "date": times[i].strftime("%d/%m/%Y") if i < len(times) else "-",
            "hour": times[i].strftime("%H:%M") if i < len(times) else "-",
        })
    return meta


def collect_figures(fig_root, runs, staging_dir):
    """Copies figures to one folder with unique names (same filename exists in every checkpoint)."""
    figures = []
    for idx, run in enumerate(runs):
        cp = run["checkpoint"]
        items = CHECKPOINT_FIGURES + (LAST_CHECKPOINT_EXTRA if idx == len(runs) - 1 else [])
        for filename, title in items:
            src = os.path.join(fig_root, f"checkpoint_{cp}", filename)
            if not os.path.exists(src):
                continue
            unique = f"cp{cp}_{filename}"
            shutil.copyfile(src, os.path.join(staging_dir, unique))
            figures.append((unique, f"נקודת ביקורת {cp}: {title}"))
    return figures


def header_xml():
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:hdr xmlns:w="{docx.NS_W}">
  {docx.para("דוח הרצות ML לפי נקודות ביקורת - generated_schedules", size=9, color="555555")}
</w:hdr>'''


def footer_xml():
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:ftr xmlns:w="{docx.NS_W}">
  <w:p>
    <w:pPr><w:jc w:val="center"/></w:pPr>
    {docx.run("דוח הרצות ML | עמוד ", size=9, color="777777")}
    <w:fldSimple w:instr="PAGE"><w:r><w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:cs="Arial"/><w:sz w:val="18"/></w:rPr><w:t>1</w:t></w:r></w:fldSimple>
  </w:p>
</w:ftr>'''


def core_xml():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>דוח הרצות ML לפי נקודות ביקורת</dc:title>
  <dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>
</cp:coreProperties>'''


def document_xml(runs, meta, figures, summary, step):
    body = []
    last = runs[-1]
    models_trained = sorted({m["model"] for r in runs for m in r["models"]})

    body.append(docx.para("דוח הרצות למידת מכונה", style="TitleStyle"))
    body.append(docx.para(f"אימון כל המודלים מחדש בכל {step} בחירות של משתמשים ובחירת המודל הטוב ביותר", style="SubtitleStyle"))
    body.append(docx.table([
        ["פרט", "ערך"],
        ["תאריך הפקת הדוח", datetime.now().strftime("%d/%m/%Y %H:%M")],
        ["מקור הנתונים", "generated_schedules"],
        ["מספר נקודות ביקורת (הרצות)", str(len(runs))],
        ["מודלים שאומנו בכל הרצה", ", ".join(models_trained)],
        ["המודל הפעיל כעת לאופטימייזר", f"{last['best_model']} (run_id {meta[-1]['run_id']})"],
    ], [4300, 5060]))

    body.append(docx.para("תקציר", style="Heading1"))
    body.append(docx.para(
        f"בכל נקודת ביקורת אומנו כל המודלים הזמינים על כל הנתונים שהצטברו עד אותו רגע, ולא רק על {step} הבחירות החדשות. "
        "מבין המודלים נבחר אוטומטית המודל בעל מדד AUC הגבוה ביותר. בשוויון ההכרעה היא לפי F1, אחריו Recall ואחריו Precision. "
        "רק המשקולות של המודל הנבחר נשמרו בטבלה ml_criterion_weights."
    ))
    body.append(docx.para(
        f"סך הכול בוצעו {len(runs)} הרצות ובכל אחת {len(models_trained)} מודלים, כלומר {len(runs) * len(models_trained)} אימונים. "
        f"נשמרו {len(runs)} שורות, אחת לכל הרצה. רק השורה האחרונה מסומנת כמודל הפעיל, והאופטימייזר בצ'אט משתמש בה."
    ))
    body.append(docx.table([["מודל", "מספר הרצות שבהן אומן", "מספר הרצות שבהן נבחר"]] + [
        [mn, str(sum(1 for r in runs if any(x["model"] == mn for x in r["models"]))),
         str(sum(1 for r in runs if r["best_model"] == mn))] for mn in models_trained
    ], [3360, 3000, 3000]))

    body.append(docx.para("איך עובד המנגנון של כל 100", style="Heading1"))
    body.append(docx.bullet("בחירה אמיתית היא סשן שבו הוצגו 2 או 3 חלופות והמשתמש בחר בדיוק אחת."))
    body.append(docx.bullet("סשן עם חלופה אחת בלבד לא נספר ולא משמש לאימון, כי לא הייתה בו השוואה בין חלופות."))
    body.append(docx.bullet("סשן בלי בחירה הוא נתון לא מתויג. הוא משמש רק לתיוג עצמי, כלומר למידה מונחית למחצה, כשהמודל בטוח מספיק."))
    body.append(docx.bullet(f"בכל פעם שמשתמש שומר מערכת, השרת בודק ברקע אם הצטברו {step} בחירות אמיתיות חדשות מאז ההרצה האחרונה. אם כן, כל המודלים מאומנים מחדש."))
    body.append(docx.bullet("קיימת גם הרצה ידנית שמאמנת מיד בלי לחכות ל-100: נקודת הקצה api/ml/train בשרת."))
    if last["checkpoint"] % step:
        body.append(docx.para(
            f"הערה: ההרצה האחרונה, אחרי {last['checkpoint']} בחירות, אינה כפולה של {step}. זו הרצת סיום על כל הבחירות שנאספו, "
            f"{last['checkpoint'] - (last['checkpoint'] // step) * step} בחירות מעבר להרצה הקודמת. "
            f"בכלל האוטומטי היא הייתה מתבצעת רק בהגעה ל-{(last['checkpoint'] // step + 1) * step} בחירות, או בהרצה ידנית.",
            italic=True,
        ))

    body.append(docx.para("נתוני הבסיס", style="Heading1"))
    if summary:
        body.append(docx.table([
            ["מדד", "ערך"],
            ["סשנים", str(summary.get("sessions"))],
            ["סשנים עם בחירה", str(summary.get("sessions_with_selection"))],
            ["סשנים עם בחירה ו-3 חלופות", str(summary.get("selected_sessions_with_3_alternatives"))],
            ["פיזור מספר החלופות בסשן", ", ".join(f"{k} חלופות: {v}" for k, v in sorted(summary.get("sessions_by_alternative_count", {}).items()))],
            ["שיחות רב-שלביות", str(summary.get("multi_turn_sessions"))],
            ["משתמשים שונים", str(summary.get("unique_students", "-"))],
            ["משתמשים שונים בסשנים עם בחירה", str(summary.get("unique_selecting_students", "-"))],
        ], [4300, 5060]))

    body.append(docx.para("סיכום ההרצות", style="Heading1"))
    rows = [["run_id", "תאריך", "בחירות אמיתיות", "מודל נבחר", "AUC", "F1"]]
    for run, m in zip(runs, meta):
        best = next(x for x in run["models"] if x["model"] == run["best_model"])
        rows.append([
            m["run_id"], m["date"], str(run["labeled_sessions"]),
            run["best_model"], fmt(best["auc"]), fmt(best["f1"]),
        ])
    body.append(docx.table(rows, [1000, 1700, 1500, 2560, 1300, 1300]))

    body.append(docx.para("התפתחות המשקולות שנבחרו", style="Heading1"))
    rows = [["קריטריון", "משמעות"] + [f"אחרי {r['checkpoint']}" for r in runs]]
    for c in CRITERIA:
        rows.append([c, CRITERION_NAMES[c]] + [fmt(r["best_weights"][c]) for r in runs])
    widths = [1000, 2600] + [int(5760 / len(runs))] * len(runs)
    body.append(docx.table(rows, widths))

    for run, m in zip(runs, meta):
        body.append(docx.page_break())
        body.append(docx.para(f"נקודת ביקורת: {run['checkpoint']} בחירות אמיתיות", style="Heading1"))
        body.append(docx.table([
            ["פרט", "ערך"],
            ["run_id", m["run_id"]],
            ["תאריך ושעת ההרצה", f"{m['date']}  {m['hour']}"],
            ["סשנים עם בחירה אמיתית", str(run["labeled_sessions"])],
            ["סשנים לא מתויגים", str(run["unlabeled_sessions"])],
            ["סשנים שקיבלו תיוג עצמי", str(run["pseudo_labeled_sessions"])],
            ["רשומות באימון הסופי", str(run["num_training_records"])],
            ["המודל שנבחר", run["best_model"]],
        ], [4300, 5060]))

        body.append(docx.para("השוואת המודלים", style="Heading2"))
        rows = [["מודל", "Accuracy", "Precision", "Recall", "F1", "AUC", "נבחר"]]
        for x in run["models"]:
            rows.append([x["model"], fmt(x["accuracy"]), fmt(x["precision"]), fmt(x["recall"]),
                         fmt(x["f1"]), fmt(x["auc"]), "✔" if x["model"] == run["best_model"] else ""])
        body.append(docx.table(rows, [2360, 1200, 1200, 1200, 1100, 1100, 1200]))

        body.append(docx.para("משקולות לפי מודל", style="Heading2"))
        model_names = list(run["weights_by_model"].keys())
        rows = [["קריטריון"] + model_names]
        for c in CRITERIA:
            rows.append([f"{c} - {CRITERION_NAMES[c]}"] + [fmt(run["weights_by_model"][mn].get(c)) for mn in model_names])
        body.append(docx.table(rows, [3360] + [int(6000 / len(model_names))] * len(model_names)))

    body.append(docx.page_break())
    body.append(docx.para("מגבלות ופרשנות", style="Heading1"))
    body.append(docx.bullet("הנתונים נוצרו בסימולציה: 400 שיחות הורצו דרך הלוגיקה האמיתית של הצ'אט (Builder ו-Optimizer). הבחירות נעשו לפי העדפות נסתרות של כל סטודנט מדומה."))
    body.append(docx.bullet("המשקולות נגזרות מחשיבות המשתנים של המודל: מקדמי הרגרסיה הלוגיסטית, או חשיבות המשתנים במודלי העצים. הן מראות כמה קריטריון עוזר להבדיל בין חלופה שנבחרה לחלופה שלא נבחרה, ולא בהכרח את כיוון ההעדפה."))
    body.append(docx.bullet("המודלים משווים חלופות מסשנים שונים זו לזו. השוואה בתוך כל סשן, כלומר נרמול לפי ממוצע הסשן, צפויה לשקף טוב יותר את העדפות המשתמשים, והיא שיפור מומלץ להמשך."))

    body.append(docx.page_break())
    body.append(docx.para("נספח גרפים", style="Heading1"))
    for idx, (filename, title) in enumerate(figures, start=1):
        if idx > 1:
            body.append(docx.page_break())
        caption = f"תרשים {idx}: {title}"
        body.append(docx.para(caption, style="Heading2"))
        body.append(docx.image_para(f"rIdImg{idx}", filename, idx, width_in=6.1))
        body.append(docx.caption(caption))

    body.append(
        '<w:sectPr>'
        '<w:headerReference w:type="default" r:id="rIdHeader1"/>'
        '<w:footerReference w:type="default" r:id="rIdFooter1"/>'
        '<w:pgSz w:w="12240" w:h="15840"/>'
        '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="708" w:footer="708" w:gutter="0"/>'
        '<w:cols w:space="720"/>'
        '</w:sectPr>'
    )

    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{docx.NS_W}" xmlns:r="{docx.NS_R}" xmlns:wp="{docx.NS_WP}" xmlns:a="{docx.NS_A}" xmlns:pic="{docx.NS_PIC}">
  <w:body>{''.join(body)}</w:body>
</w:document>'''


def build(data_dir, out_path, step):
    fig_root = os.path.join(data_dir, "analytics_outputs")
    runs = load_json(os.path.join(fig_root, "checkpoint_runs.json"))
    summary_path = os.path.join(data_dir, "summary.json")
    summary = load_json(summary_path) if os.path.exists(summary_path) else None
    sessions_path = os.path.join(data_dir, "chat_sessions.json")
    personas_path = os.path.join(data_dir, "personas.json")
    if summary is not None and os.path.exists(sessions_path):
        summary["unique_students"] = len({r["student_id"] for r in load_json(sessions_path)})
        if os.path.exists(personas_path):
            summary["unique_selecting_students"] = len({r["student_id"] for r in load_json(personas_path) if r.get("selected_alt")})
    meta = run_meta(data_dir, runs)

    staging = tempfile.mkdtemp(prefix="ml_report_figs_")
    try:
        figures = collect_figures(fig_root, runs, staging)
        docx.FIG_DIR = staging
        files = [f for f, _ in figures]

        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", docx.content_types_xml(files))
            z.writestr("_rels/.rels", docx.root_rels_xml())
            z.writestr("word/document.xml", document_xml(runs, meta, figures, summary, step))
            z.writestr("word/_rels/document.xml.rels", docx.rels_xml(files))
            z.writestr("word/styles.xml", docx.styles_xml())
            z.writestr("word/header1.xml", header_xml())
            z.writestr("word/footer1.xml", footer_xml())
            z.writestr("docProps/core.xml", core_xml())
            z.writestr("docProps/app.xml", docx.app_xml())
            for filename in files:
                with open(os.path.join(staging, filename), "rb") as f:
                    z.writestr(f"word/media/{filename}", f.read())
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    print(os.path.abspath(out_path))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.join(PROJECT_DIR, "simulation_output"))
    ap.add_argument("--step", type=int, default=100)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    out = args.out or os.path.join(PROJECT_DIR, f"ml_checkpoint_runs_report_he_{datetime.now().strftime('%Y%m%d_%H%M%S')}.docx")
    build(args.dir, out, args.step)
