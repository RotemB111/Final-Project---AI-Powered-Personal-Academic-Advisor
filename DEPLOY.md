# העלאת האתר לאוויר – חינם

המבנה:

| חלק | איפה רץ | עלות |
|---|---|---|
| האתר (קבצי ה-HTML) | GitHub Pages | חינם |
| שרת הצ'אט (`chat.py`) | Render | חינם |
| מסד הנתונים | Supabase (קיים) | חינם |
| Gemini | כבוי (`USE_GEMINI=false`) | — |

הסדר חשוב: קודם Supabase, אחר כך GitHub, אחר כך Render, ובסוף מחברים ביניהם.

---

## שלב 1 – נעילת הטבלאות ב-Supabase (פעם אחת)

1. נכנסים ל-https://supabase.com/dashboard ובוחרים את הפרויקט.
2. בתפריט הצד: **SQL Editor** ← **New query**.
3. מדביקים את כל התוכן של הקובץ `supabase_security.sql` ולוחצים **Run**.
4. בטבלה שמופיעה למטה, בכל השורות צריך להיות `rowsecurity = true`.

מה זה עושה: הדפדפן כבר לא יכול לקרוא את טבלת הסטודנטים (והסיסמאות). השרת ממשיך לעבוד כרגיל.

---

## שלב 2 – יצירת ה-Repository ב-GitHub

1. נכנסים ל-https://github.com/new
2. **Repository name**: למשל `course-scheduler`
3. בוחרים **Public** (חובה כדי ש-GitHub Pages יהיה חינמי).
4. **לא** מסמנים README / .gitignore / license (הם כבר קיימים בפרויקט).
5. לוחצים **Create repository** ומעתיקים את הכתובת (למשל `https://github.com/USERNAME/course-scheduler.git`).

העלאת הקבצים (Git כבר מוכן בתיקייה עם commit ראשון). בטרמינל בתיקיית הפרויקט:

```
git remote add origin https://github.com/USERNAME/course-scheduler.git
git push -u origin main
```

בפעם הראשונה ייפתח חלון התחברות ל-GitHub בדפדפן.

> הקבצים `.env`, `.zip`, `Data/` ותוצרי הסימולציה **לא** עולים – הם חסומים ב-`.gitignore`.

---

## שלב 3 – הפעלת שרת הצ'אט ב-Render

1. נרשמים ב-https://render.com עם חשבון ה-GitHub (Sign in with GitHub). לא צריך כרטיס אשראי.
2. **New +** ← **Blueprint**.
3. בוחרים את ה-repository `course-scheduler` ← Render מזהה את הקובץ `render.yaml`.
4. Render יבקש שני ערכים – מעתיקים אותם מהקובץ `.env` במחשב:
   - `SUPABASE_URL`
   - `SUPABASE_KEY`
5. לוחצים **Apply**. הבנייה הראשונה לוקחת כ-5 דקות.
6. בסיום, בעמוד השירות מופיעה הכתובת, למשל `https://course-scheduler-api.onrender.com`.
7. בדיקה: פותחים בדפדפן `<הכתובת>/api/health` – צריך להופיע `{"status":"ok"}`.

---

## שלב 4 – חיבור האתר לשרת

אם הכתובת של Render **שונה** מ-`https://course-scheduler-api.onrender.com`:

1. ב-GitHub פותחים את הקובץ `config.js` ← עיפרון (Edit).
2. מחליפים את הכתובת בשורה `const RENDER_API_URL = "...";` בכתובת מ-Render.
3. **Commit changes**.
4. גם ב-GitHub: **Settings** ← **Secrets and variables** ← **Actions** ← לשונית **Variables** ← **New repository variable**:
   שם `API_URL`, ערך: הכתובת מ-Render (לשמירת השרת והמסד ערים).

אם הכתובת זהה – אין מה לשנות.

---

## שלב 5 – הפעלת GitHub Pages

1. ב-repository: **Settings** ← **Pages**.
2. **Source**: `Deploy from a branch`.
3. **Branch**: `main`, תיקייה `/ (root)` ← **Save**.
4. אחרי דקה-שתיים מופיעה הכתובת: `https://USERNAME.github.io/course-scheduler/`

זה הקישור לשתף. כל מי שנכנס יכול להירשם, או להשתמש במשתמש לדוגמה:
**noa.cohen0001** / **demo1234**

---

## שלב 6 – שמירה על השירותים ערים (אוטומטי)

הקובץ `.github/workflows/keep-alive.yml` שולח פעם ביום בקשה לשרת, והשרת מבצע שאילתה אחת ב-Supabase.
כך פרויקט Supabase החינמי לא מושהה אחרי 7 ימים בלי פעילות.

- בדיקה ידנית: לשונית **Actions** ← **Keep alive** ← **Run workflow**.
- GitHub מכבה תזמונים ב-repository שלא היה בו שום commit במשך 60 יום. אם זה קורה, מופיע כפתור **Enable workflow** באותו מקום.

---

## מגבלות החינם – מה לצפות

| מה | מה קורה |
|---|---|
| Render נרדם אחרי 15 דקות בלי שימוש | הבקשה הראשונה אחרי הפסקה לוקחת 30–50 שניות. אחר כך מהיר |
| Render: 512MB זיכרון | מספיק לצ'אט ולאימון המודלים כל 100 בחירות |
| הגרפים של אימון ה-ML | לא נשמרים בשרת (הדיסק שם זמני). הגרפים והדוח נוצרים מקומית |
| Supabase חינמי | 500MB מסד נתונים – הרבה מעבר לנדרש |

---

## עבודה מקומית (כמו קודם)

```
pip install -r requirements-dev.txt
py -m uvicorn chat:app --reload --port 8000
```

ופותחים את `index.html`. כשהאתר נפתח מהמחשב, הוא פונה אוטומטית ל-`http://localhost:8000`.

- Gemini כבוי כברירת מחדל. להפעלה מקומית מוסיפים ל-`.env`: `USE_GEMINI=true`
- כל push ל-`main` מעדכן אוטומטית גם את האתר וגם את השרת ב-Render.
