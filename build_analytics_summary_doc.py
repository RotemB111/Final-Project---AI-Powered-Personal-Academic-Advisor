import html
import os
import struct
import zipfile
from datetime import date


OUT_DOCX = "analytics_weights_adjustment_summary_he.docx"
FIG_DIR = "analytics_outputs"

NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
NS_PIC = "http://schemas.openxmlformats.org/drawingml/2006/picture"
NS_REL = "http://schemas.openxmlformats.org/package/2006/relationships"

EMU_PER_INCH = 914400


def x(text):
    return html.escape(str(text), quote=False)


def png_size(path):
    with open(path, "rb") as f:
        sig = f.read(8)
        if sig != b"\x89PNG\r\n\x1a\n":
            raise ValueError(f"Not a PNG file: {path}")
        length, chunk_type = struct.unpack(">I4s", f.read(8))
        if chunk_type != b"IHDR":
            raise ValueError(f"Missing IHDR chunk: {path}")
        width, height = struct.unpack(">II", f.read(8))
    return width, height


def ppr(style=None, align="right", before=None, after=None, bidi=True, keep_next=False):
    parts = ["<w:pPr>"]
    if style:
        parts.append(f'<w:pStyle w:val="{style}"/>')
    if keep_next:
        parts.append("<w:keepNext/>")
    if before is not None or after is not None:
        attrs = []
        if before is not None:
            attrs.append(f'w:before="{int(before * 20)}"')
        if after is not None:
            attrs.append(f'w:after="{int(after * 20)}"')
        parts.append(f"<w:spacing {' '.join(attrs)}/>")
    if align:
        parts.append(f'<w:jc w:val="{align}"/>')
    if bidi:
        parts.append("<w:bidi/>")
    parts.append("</w:pPr>")
    return "".join(parts)


def run(text, bold=False, italic=False, size=None, color=None, rtl=True):
    rpr = []
    if bold:
        rpr.append("<w:b/>")
    if italic:
        rpr.append("<w:i/>")
    if rtl:
        rpr.append("<w:rtl/>")
    rpr.append('<w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:cs="Arial"/>')
    if size:
        rpr.append(f'<w:sz w:val="{int(size * 2)}"/><w:szCs w:val="{int(size * 2)}"/>')
    if color:
        rpr.append(f'<w:color w:val="{color}"/>')
    return f"<w:r><w:rPr>{''.join(rpr)}</w:rPr><w:t>{x(text)}</w:t></w:r>"


def para(text="", style=None, align="right", before=None, after=None, bold=False, italic=False, size=None, color=None, keep_next=False):
    return (
        "<w:p>"
        + ppr(style=style, align=align, before=before, after=after, keep_next=keep_next)
        + run(text, bold=bold, italic=italic, size=size, color=color)
        + "</w:p>"
    )


def bullet(text):
    return (
        "<w:p>"
        + ppr(style="BulletList", align="right", after=4)
        + run(text)
        + "</w:p>"
    )


def page_break():
    return '<w:p><w:r><w:br w:type="page"/></w:r></w:p>'


def cell(text, width, bold=False, fill=None, align="right", color=None):
    shading = f'<w:shd w:fill="{fill}"/>' if fill else ""
    return (
        "<w:tc>"
        f'<w:tcPr><w:tcW w:w="{width}" w:type="dxa"/>{shading}'
        '<w:tcMar><w:top w:w="80" w:type="dxa"/><w:bottom w:w="80" w:type="dxa"/>'
        '<w:start w:w="120" w:type="dxa"/><w:end w:w="120" w:type="dxa"/></w:tcMar>'
        "</w:tcPr>"
        "<w:p>"
        + ppr(style="TableText", align=align, after=0)
        + run(text, bold=bold, color=color)
        + "</w:p>"
        "</w:tc>"
    )


def table(rows, widths, header=True):
    total = sum(widths)
    grid = "".join(f'<w:gridCol w:w="{w}"/>' for w in widths)
    out = [
        "<w:tbl>",
        '<w:tblPr><w:tblStyle w:val="TableGrid"/>'
        f'<w:tblW w:w="{total}" w:type="dxa"/><w:tblInd w:w="120" w:type="dxa"/>'
        '<w:tblBorders><w:top w:val="single" w:sz="4" w:color="D7DBE2"/>'
        '<w:left w:val="single" w:sz="4" w:color="D7DBE2"/>'
        '<w:bottom w:val="single" w:sz="4" w:color="D7DBE2"/>'
        '<w:right w:val="single" w:sz="4" w:color="D7DBE2"/>'
        '<w:insideH w:val="single" w:sz="4" w:color="D7DBE2"/>'
        '<w:insideV w:val="single" w:sz="4" w:color="D7DBE2"/></w:tblBorders>'
        '<w:tblCellMar><w:top w:w="80" w:type="dxa"/><w:bottom w:w="80" w:type="dxa"/>'
        '<w:start w:w="120" w:type="dxa"/><w:end w:w="120" w:type="dxa"/></w:tblCellMar>'
        "</w:tblPr>",
        f"<w:tblGrid>{grid}</w:tblGrid>",
    ]
    for i, row in enumerate(rows):
        is_header = header and i == 0
        out.append("<w:tr>")
        if is_header:
            out.append("<w:trPr><w:tblHeader/></w:trPr>")
        for text, width in zip(row, widths):
            out.append(cell(text, width, bold=is_header, fill="F2F4F7" if is_header else None, align="center" if is_header else "right"))
        out.append("</w:tr>")
    out.append("</w:tbl>")
    out.append(para("", after=6))
    return "".join(out)


def image_para(rel_id, filename, image_id, width_in=6.1):
    path = os.path.join(FIG_DIR, filename)
    px_w, px_h = png_size(path)
    width_emu = int(width_in * EMU_PER_INCH)
    height_emu = int(width_emu * px_h / px_w)
    name = x(filename)
    return f"""
<w:p>
  <w:pPr><w:jc w:val="center"/></w:pPr>
  <w:r>
    <w:drawing>
      <wp:inline distT="0" distB="0" distL="0" distR="0">
        <wp:extent cx="{width_emu}" cy="{height_emu}"/>
        <wp:effectExtent l="0" t="0" r="0" b="0"/>
        <wp:docPr id="{image_id}" name="{name}"/>
        <wp:cNvGraphicFramePr>
          <a:graphicFrameLocks noChangeAspect="1"/>
        </wp:cNvGraphicFramePr>
        <a:graphic>
          <a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">
            <pic:pic>
              <pic:nvPicPr>
                <pic:cNvPr id="{image_id}" name="{name}"/>
                <pic:cNvPicPr/>
              </pic:nvPicPr>
              <pic:blipFill>
                <a:blip r:embed="{rel_id}"/>
                <a:stretch><a:fillRect/></a:stretch>
              </pic:blipFill>
              <pic:spPr>
                <a:xfrm><a:off x="0" y="0"/><a:ext cx="{width_emu}" cy="{height_emu}"/></a:xfrm>
                <a:prstGeom prst="rect"><a:avLst/></a:prstGeom>
              </pic:spPr>
            </pic:pic>
          </a:graphicData>
        </a:graphic>
      </wp:inline>
    </w:drawing>
  </w:r>
</w:p>
"""


def styles_xml():
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="{NS_W}">
  <w:docDefaults>
    <w:rPrDefault><w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:cs="Arial"/><w:sz w:val="22"/><w:szCs w:val="22"/></w:rPr></w:rPrDefault>
    <w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="264" w:lineRule="auto"/><w:bidi/></w:pPr></w:pPrDefault>
  </w:docDefaults>
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal">
    <w:name w:val="Normal"/><w:qFormat/>
    <w:pPr><w:spacing w:after="120" w:line="264" w:lineRule="auto"/><w:jc w:val="right"/><w:bidi/></w:pPr>
    <w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:cs="Arial"/><w:sz w:val="22"/><w:szCs w:val="22"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="TitleStyle">
    <w:name w:val="TitleStyle"/><w:basedOn w:val="Normal"/><w:qFormat/>
    <w:pPr><w:spacing w:before="0" w:after="120"/><w:jc w:val="right"/><w:bidi/></w:pPr>
    <w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:cs="Arial"/><w:b/><w:color w:val="0B2545"/><w:sz w:val="48"/><w:szCs w:val="48"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="SubtitleStyle">
    <w:name w:val="SubtitleStyle"/><w:basedOn w:val="Normal"/><w:qFormat/>
    <w:pPr><w:spacing w:after="240"/><w:jc w:val="right"/><w:bidi/></w:pPr>
    <w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:cs="Arial"/><w:color w:val="555555"/><w:sz w:val="27"/><w:szCs w:val="27"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:qFormat/>
    <w:pPr><w:spacing w:before="240" w:after="120"/><w:keepNext/><w:jc w:val="right"/><w:bidi/></w:pPr>
    <w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:cs="Arial"/><w:b/><w:color w:val="2E74B5"/><w:sz w:val="32"/><w:szCs w:val="32"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading2">
    <w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:qFormat/>
    <w:pPr><w:spacing w:before="200" w:after="100"/><w:keepNext/><w:jc w:val="right"/><w:bidi/></w:pPr>
    <w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:cs="Arial"/><w:b/><w:color w:val="2E74B5"/><w:sz w:val="26"/><w:szCs w:val="26"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading3">
    <w:name w:val="heading 3"/><w:basedOn w:val="Normal"/><w:qFormat/>
    <w:pPr><w:spacing w:before="160" w:after="80"/><w:keepNext/><w:jc w:val="right"/><w:bidi/></w:pPr>
    <w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:cs="Arial"/><w:b/><w:color w:val="1F4D78"/><w:sz w:val="24"/><w:szCs w:val="24"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="CaptionStyle">
    <w:name w:val="CaptionStyle"/><w:basedOn w:val="Normal"/><w:qFormat/>
    <w:pPr><w:spacing w:before="80" w:after="160"/><w:jc w:val="center"/><w:bidi/></w:pPr>
    <w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:cs="Arial"/><w:i/><w:color w:val="555555"/><w:sz w:val="19"/><w:szCs w:val="19"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="TableText">
    <w:name w:val="TableText"/><w:basedOn w:val="Normal"/>
    <w:pPr><w:spacing w:after="0" w:line="264" w:lineRule="auto"/><w:jc w:val="right"/><w:bidi/></w:pPr>
    <w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:cs="Arial"/><w:sz w:val="20"/><w:szCs w:val="20"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="BulletList">
    <w:name w:val="BulletList"/><w:basedOn w:val="Normal"/>
    <w:pPr><w:spacing w:after="80" w:line="280" w:lineRule="auto"/><w:ind w:right="720" w:hanging="360"/><w:jc w:val="right"/><w:bidi/></w:pPr>
    <w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:cs="Arial"/><w:sz w:val="22"/><w:szCs w:val="22"/></w:rPr>
  </w:style>
</w:styles>"""


def rels_xml(image_files):
    rels = [
        f'<Relationship Id="rIdHeader1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" Target="header1.xml"/>',
        f'<Relationship Id="rIdFooter1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/>',
    ]
    for i, filename in enumerate(image_files, start=1):
        rels.append(
            f'<Relationship Id="rIdImg{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/{x(filename)}"/>'
        )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{NS_REL}">{''.join(rels)}</Relationships>"""


def root_rels_xml():
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{NS_REL}">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""


def content_types_xml(image_files):
    png_default = '<Default Extension="png" ContentType="image/png"/>'
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  {png_default}
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
  <Override PartName="/word/header1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/>
  <Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>"""


def core_xml():
    today = date.today().isoformat()
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>סיכום התאמת משקולות האנליטיקה</dc:title>
  <dc:creator>Codex</dc:creator>
  <cp:lastModifiedBy>Codex</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">{today}T00:00:00Z</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{today}T00:00:00Z</dcterms:modified>
</cp:coreProperties>"""


def app_xml():
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>Codex</Application>
</Properties>"""


def header_xml():
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:hdr xmlns:w="{NS_W}">
  {para("דוח התאמת משקולות ML - generated_schedules", size=9, color="555555")}
</w:hdr>"""


def footer_xml():
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:ftr xmlns:w="{NS_W}">
  <w:p>
    <w:pPr><w:jc w:val="center"/></w:pPr>
    {run("דוח משקולות ML | עמוד ", size=9, color="777777")}
    <w:fldSimple w:instr="PAGE"><w:r><w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:cs="Arial"/><w:sz w:val="18"/></w:rPr><w:t>1</w:t></w:r></w:fldSimple>
  </w:p>
</w:ftr>"""


def caption(text):
    return para(text, style="CaptionStyle", align="center")


def document_xml(image_files):
    body = []
    body.append(para("סיכום התאמת משקולות האנליטיקה", style="TitleStyle"))
    body.append(para("ניתוח תוצאות המודל על טבלת generated_schedules והטמעת המשקולות באופטימייזר", style="SubtitleStyle"))
    body.append(table([
        ["שדה", "ערך"],
        ["תאריך הרצה", "20 ביוני 2026"],
        ["טבלה שנותחה", "generated_schedules"],
        ["טבלת שמירת משקולות", "ml_criterion_weights"],
        ["מודל שנבחר", "Random Forest"],
        ["מצב הרצה", "קריאה מה-DB בלבד; לא נשמרה רשומה חדשה ל-ml_criterion_weights"],
    ], [2500, 6860]))

    body.append(para("תקציר מנהלים", style="Heading1"))
    body.append(para("הפייפליין רץ מול Supabase, קרא 536 רשומות של מערכות שנוצרו, וסיווג 155 סשנים כסשנים תקינים לאימון: בכל אחד מהם בדיוק מערכת אחת סומנה כבחירה של המשתמש. לאחר תיקון ולידציה לפי session_id, המודל שנבחר היה Random Forest. המודל הגיע ל-AUC סופי של 0.701 לאחר שימוש זהיר ב-semi-supervised pseudo-labeling."))
    body.append(para("המשמעות העיקרית של ההתאמה: המשקולות הישנות נתנו משקל גבוה מאוד למספר ימי הלימוד ולשעת הלימוד. המודל למד מהבחירות בפועל שהקריטריונים Gap windows, Daily load, Mandatory attendance ו-Course assessment load מסבירים טוב יותר את הבחירות היחסיות של המשתמשים."))

    body.append(para("משקולות: לפני ואחרי", style="Heading1"))
    weights_rows = [
        ["קריטריון", "משמעות", "לפני", "אחרי", "שינוי"],
        ["G", "חלונות/פערים במערכת", "0.0700", "0.2284", "+0.1584"],
        ["D", "מספר ימי לימוד פעילים", "0.3700", "0.0966", "-0.2734"],
        ["LD", "עומס יומי", "0.1600", "0.1801", "+0.0201"],
        ["T", "התאמה להעדפת שעות", "0.2300", "0.1105", "-0.1195"],
        ["LW", "איזון עומס שבועי", "0.1000", "0.1135", "+0.0135"],
        ["C", "עומס רכיבי הערכה בקורסים", "0.0500", "0.1321", "+0.0821"],
        ["A", "חובת נוכחות", "0.0200", "0.1388", "+0.1188"],
    ]
    body.append(table(weights_rows, [1100, 3260, 1250, 1250, 2500]))
    body.append(para("המשקולות החדשות הוטמעו ב-optimizer.py. הערך של D עודכן ל-0.0966 במקום 0.0965 כדי שסכום המשקולות המעוגל יהיה בדיוק 1.0.", italic=True, color="555555"))

    body.append(para("איכות הנתונים", style="Heading1"))
    body.append(table([
        ["מדד", "תוצאה"],
        ["סך רשומות", "536"],
        ["סך סשנים", "189"],
        ["סשנים תקינים עם בדיוק בחירה אחת", "155"],
        ["סשנים ללא בחירה", "34"],
        ["סשנים עם יותר מבחירה אחת", "0"],
        ["סשנים עם יותר מ-3 מערכות", "0"],
        ["רשומות חיוביות בסשנים התקינים", "155"],
        ["רשומות שליליות בסשנים התקינים", "297"],
    ], [3900, 5460]))
    body.append(para("ממצא איכות מרכזי: אין סשנים פסולים לפי הכללים שנבדקו. הבעיה היחידה היא 34 סשנים ללא בחירה, שאינם מלמדים בהכרח שהמערכות לא טובות ולכן לא הוגדרו כשליליים."))

    body.append(para("ביצועי המודלים", style="Heading1"))
    body.append(table([
        ["שלב", "מודל", "Accuracy", "Precision", "Recall", "F1", "AUC"],
        ["אימון מפוקח בלבד", "Logistic Regression", "0.578", "0.434", "0.742", "0.548", "0.574"],
        ["אימון מפוקח בלבד", "Random Forest", "0.644", "0.484", "0.484", "0.484", "0.682"],
        ["אחרי pseudo-labeling", "Logistic Regression", "0.474", "0.364", "0.727", "0.485", "0.524"],
        ["אחרי pseudo-labeling", "Random Forest", "0.608", "0.444", "0.606", "0.513", "0.701"],
    ], [1900, 2300, 1030, 1030, 1030, 1030, 1040]))
    body.append(para("המודל הסופי נבחר לפי AUC, ולאחר מכן F1/Recall/Precision. למרות ש-Accuracy ירד מעט אחרי pseudo-labeling, ה-AUC עלה ל-0.701 וה-F1 של Random Forest השתפר ל-0.513, ולכן הוא נבחר כבסיס למשקולות."))

    body.append(para("ממצאים עיקריים", style="Heading1"))
    for item in [
        "הקורלציות הישירות בין קריטריון יחיד לבין בחירה הן חלשות יחסית. זה מחזק את הצורך במודל שמסתכל על שילוב הקריטריונים ולא על קורלציה חד-ממדית.",
        "הקשר החזק ביותר בתוך מטריצת הקורלציה הוא בין C ל-A בערך -0.697. כלומר, שני הקריטריונים אינם בלתי תלויים לגמרי וצריך להיזהר בפרשנות נפרדת שלהם.",
        "G הפך לקריטריון המשמעותי ביותר במשקולות החדשות. בפועל, המשתמשים כנראה מעדיפים מערכות עם פחות פערים בעייתיים יותר ממה שהוגדר ידנית קודם.",
        "D ירד בצורה חדה. מספר ימי לימוד עדיין חשוב, אבל הבחירות בפועל לא תמכו במשקל ידני גבוה של 0.370.",
        "A ו-C עלו משמעותית, מה שמרמז שמבנה הקורסים וחובת הנוכחות משפיעים על בחירת מערכת יותר מכפי שהאופטימייזר הישן הניח.",
    ]:
        body.append(bullet(item))

    body.append(para("תיקונים שבוצעו בפייפליין", style="Heading1"))
    for item in [
        "תוקנה בעיית UTF-8/emoji ב-Windows PowerShell כדי שהסקריפט ירוץ בלי UnicodeEncodeError.",
        "הוגדר MPLCONFIGDIR בתוך תיקיית הפרויקט, כדי למנוע כשלי הרשאות של Matplotlib בתיקיית הבית.",
        "SHOW_PLOTS הוגדר כברירת מחדל ל-False כדי שהרצה אוטומטית לא תיתקע על חלונות גרפיים.",
        "נוסף דפדוף ב-Supabase באמצעות range, כדי שהפייפליין לא יסתמך על עמוד תוצאות יחיד.",
        "כיוון ה-validation תוקן כך שגם GridSearchCV של Random Forest ישמור קבוצות לפי session_id באמצעות StratifiedGroupKFold כשזמין.",
        "המשקולות החדשות עודכנו בקוד האופטימייזר.",
    ]:
        body.append(bullet(item))

    body.append(para("המלצות המשך", style="Heading1"))
    for item in [
        "להמשיך לאסוף סשנים מסומנים. 155 סשנים תקינים מספיקים להרצה ראשונית, אבל AUC של 0.701 עדיין מצביע על מודל בינוני ולא על ודאות גבוהה.",
        "לשמור את תוצאות ההרצה גם ב-ml_criterion_weights כאשר מחליטים שזו גרסה רשמית לפרודקשן.",
        "להוסיף xgboost ל-requirements.txt רק אם רוצים להשוות גם XGBoost; בהרצה הנוכחית הוא לא היה מותקן ולכן דולג.",
        "להמשיך להשתמש ב-validation לפי session_id. ערבוב חלופות מאותו סשן בין train/test עלול להציג ביצועים אופטימיים מדי.",
        "לבחון מחדש את המשקולות אחרי עוד נתונים, במיוחד אם חוויית הבחירה בממשק משתנה או אם האופטימייזר מתחיל לייצר חלופות מסוג חדש.",
    ]:
        body.append(bullet(item))

    body.append(page_break())
    body.append(para("נספח גרפים", style="Heading1"))

    captions = {
        "00_correlation_matrix.png": "תרשים 1: מטריצת קורלציה בין הקריטריונים לבין בחירת המשתמש.",
        "01_data_quality_summary.png": "תרשים 2: סיכום איכות הנתונים לפי סוג סשן.",
        "02_model_performance_comparison.png": "תרשים 3: השוואת ביצועי המודלים.",
        "03_criterion_importance_by_model.png": "תרשים 4: השוואת חשיבות הקריטריונים לפי מודל.",
        "04_final_optimizer_weights.png": "תרשים 5: המשקולות הסופיות שהוטמעו באופטימייזר.",
        "05_roc_curve_comparison.png": "תרשים 6: עקומות ROC להשוואת המודלים.",
        "06_confusion_matrix_logistic_regression.png": "תרשים 7: מטריצת בלבול - Logistic Regression.",
        "06_confusion_matrix_random_forest.png": "תרשים 8: מטריצת בלבול - Random Forest.",
    }

    for idx, filename in enumerate(image_files, start=1):
        if idx > 1:
            body.append(page_break())
        body.append(para(captions[filename], style="Heading2"))
        body.append(image_para(f"rIdImg{idx}", filename, idx, width_in=6.1))
        body.append(caption(captions[filename]))

    body.append(
        '<w:sectPr>'
        '<w:headerReference w:type="default" r:id="rIdHeader1"/>'
        '<w:footerReference w:type="default" r:id="rIdFooter1"/>'
        '<w:pgSz w:w="12240" w:h="15840"/>'
        '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="708" w:footer="708" w:gutter="0"/>'
        '<w:cols w:space="720"/>'
        '</w:sectPr>'
    )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{NS_W}" xmlns:r="{NS_R}" xmlns:wp="{NS_WP}" xmlns:a="{NS_A}" xmlns:pic="{NS_PIC}">
  <w:body>{''.join(body)}</w:body>
</w:document>"""


def build():
    image_files = [
        "00_correlation_matrix.png",
        "01_data_quality_summary.png",
        "02_model_performance_comparison.png",
        "03_criterion_importance_by_model.png",
        "04_final_optimizer_weights.png",
        "05_roc_curve_comparison.png",
        "06_confusion_matrix_logistic_regression.png",
        "06_confusion_matrix_random_forest.png",
    ]
    for filename in image_files:
        path = os.path.join(FIG_DIR, filename)
        if not os.path.exists(path):
            raise FileNotFoundError(path)

    with zipfile.ZipFile(OUT_DOCX, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types_xml(image_files))
        z.writestr("_rels/.rels", root_rels_xml())
        z.writestr("word/document.xml", document_xml(image_files))
        z.writestr("word/_rels/document.xml.rels", rels_xml(image_files))
        z.writestr("word/styles.xml", styles_xml())
        z.writestr("word/header1.xml", header_xml())
        z.writestr("word/footer1.xml", footer_xml())
        z.writestr("docProps/core.xml", core_xml())
        z.writestr("docProps/app.xml", app_xml())
        for filename in image_files:
            with open(os.path.join(FIG_DIR, filename), "rb") as f:
                z.writestr(f"word/media/{filename}", f.read())
    print(os.path.abspath(OUT_DOCX))


if __name__ == "__main__":
    build()
