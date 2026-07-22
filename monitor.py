import time
import requests
import xml.etree.ElementTree as ET
from urllib.parse import quote
from bs4 import BeautifulSoup
import os
import smtplib
from email.message import EmailMessage
import re
from dotenv import load_dotenv

# Load the variables from the .env file into the system environment
load_dotenv()

# ==========================
# CONFIGURATION
# ==========================

PROXY_USER = os.environ["IITM_USER"]
PROXY_PASS = os.environ["IITM_PASS"]
NTFY_TOPIC = os.environ["NTFY_TOPIC"]

GMAIL = os.environ["MAIL_USER"]
APP_PASSWORD = os.environ["MAIL_APP_PASSWORD"]
TO = os.environ["MAIL_TO"]

PERIOD = os.getenv("PERIOD", "123")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "10"))
HEARTBEAT_INTERVAL = 3 * 60 * 60

WATCH_REGEX = os.getenv("WATCH_REGEX")
if WATCH_REGEX:
    WATCH_PATTERN = re.compile(WATCH_REGEX)
    WATCH_COURSES = []
else:
    WATCH_PATTERN = None
    watch_courses_env = os.getenv("WATCH_COURSES", "")
    WATCH_COURSES = [c.strip() for c in watch_courses_env.split(",") if c.strip()]

# ==========================
# PROXY
# ==========================

encoded_password = quote(PROXY_PASS, safe="")

PROXY_URL = f"https://{PROXY_USER}:{encoded_password}@remote.iitm.ac.in:8372"

session = requests.Session()

session.proxies = {
    "http": PROXY_URL,
    "https": PROXY_URL,
}

LOGIN_URL = "https://workflow.iitm.ac.in/student/"
AUTH_URL = "https://workflow.iitm.ac.in/student/Authenticate.aspx"

LOGIN_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:141.0) Gecko/20100101 Firefox/141.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": LOGIN_URL,
}

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/json; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://workflow.iitm.ac.in",
    "Referer": "https://workflow.iitm.ac.in/student/",
}

URL = (
    "https://workflow.iitm.ac.in/student/"
    "WebServices/CurriculumServices.asmx/"
    "GetElectiveCoursesDetailsAddDrop"
)


# ==========================
# API
# ==========================
def workflow_login(workflow_password):
    r = session.get(LOGIN_URL, headers=LOGIN_HEADERS)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")

    def value(name):
        tag = soup.find("input", attrs={"name": name})
        if tag is None:
            tag = soup.find(id=name)
        return "" if tag is None else tag.get("value", "")

    payload = {
        "ScriptManager1_HiddenField": value("ScriptManager1_HiddenField"),
        "__EVENTTARGET": "",
        "__EVENTARGUMENT": "",
        "__VIEWSTATE": value("__VIEWSTATE"),
        "__VIEWSTATEGENERATOR": value("__VIEWSTATEGENERATOR"),
        "__EVENTVALIDATION": value("__EVENTVALIDATION"),
        "txtUserName": PROXY_USER,
        "txtPassword": workflow_password,
        "HiddenCaptcha": value("HiddenCaptcha"),
        "txtCaptcha": value("HiddenCaptcha"),
        "Login": "Login",
    }

    r = session.post(
        AUTH_URL,
        data=payload,
        headers=LOGIN_HEADERS,
        allow_redirects=True,
    )

    r.raise_for_status()

    if "StudentBasicInfo.aspx" not in r.url:
        raise RuntimeError("Workflow login failed.")

    print("✓ Logged into Workflow")


def fetch_courses():
    try:
        response = session.post(
            URL,
            json={"period": PERIOD},
            headers=HEADERS,
            timeout=30,
        )

        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "")

        if "application/json" not in content_type:
            raise RuntimeError("Session expired")

        xml = response.json()["d"]
        return ET.fromstring(xml)

    except Exception as e:
        print("API failed:", e)
        print("Logging in again...")

        workflow_login(PROXY_PASS)

        response = session.post(
            URL,
            json={"period": PERIOD},
            headers=HEADERS,
            timeout=30,
        )

        response.raise_for_status()
        xml = response.json()["d"]
        return ET.fromstring(xml)


# ==========================
# NOTIFICATIONS
# ==========================

def notify(course, name, vacancies, slot):
    try:
        ntfy_notify(course, name, vacancies, slot)
    except Exception as e:
        print(f"ntfy failed: {e}")

    try:
        email_notify(course, name, vacancies, slot)
    except Exception as e:
        print(f"email failed: {e}")


def email_notify(course, name, vacancies, slot):
    msg = EmailMessage()
    msg["Subject"] = f"Workflow Vacancy - {course}"
    msg["From"] = GMAIL
    msg["To"] = TO

    msg.set_content(
        f"""
Course    : {course}
Name      : {name}
Vacancies : {vacancies}
Slot      : {slot}

Workflow:
https://workflow.iitm.ac.in/student/
"""
    )

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as smtp:
        smtp.login(GMAIL, APP_PASSWORD)
        smtp.send_message(msg)


def ntfy_notify(course, name, vacancies, slot):
    r = requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=(
            f"🎉 {course} is available!\n\n"
            f"{name}\n"
            f"Vacancies: {vacancies}\n"
            f"Slot: {slot}"
        ).encode("utf-8"),
        headers={
            "Title": "Workflow Vacancy",
            "Priority": "urgent",
            "Tags": "tada,books",
        },
        timeout=(10, 10),
    )
    r.raise_for_status()


def heartbeat(last_seen):
    lines = []
    
    courses_to_report = last_seen.keys() if WATCH_REGEX else WATCH_COURSES

    for course in courses_to_report:
        status = last_seen.get(course)
        if status == "NOT_FOUND":
            lines.append(f"{course}: Not Found")
        elif status is None:
            lines.append(f"{course}: Unknown")
        else:
            lines.append(f"{course}: {status} vacancies")

    message = "✅ Workflow Monitor Alive\n\n" + "\n".join(lines)

    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": "Workflow Heartbeat"},
            timeout=10,
        )
    except Exception as e:
        print(f"Heartbeat failed: {e}")


# ==========================
# PARSER
# ==========================

def get_all_courses(root):
    courses = {}

    for course in root.iter():
        if not course.tag.endswith("ElectiveCoursesAvail"):
            continue

        data = {}
        for child in course:
            tag = child.tag.split("}")[-1]
            data[tag] = (child.text or "").strip()

        courses[data["CourseNo"]] = {
            "name": data["CourseName"],
            "slot": data["Slot"],
            "vacancies": int(data["AvailableVacancies"]),
        }

    return courses


# ==========================
# MONITOR
# ==========================

def monitor():
    print("Watching:")
    if WATCH_REGEX:
        print(f"  • Regex pattern: {WATCH_REGEX}")
    else:
        for c in WATCH_COURSES:
            print(f"  • {c}")
    print()

    last_seen = {}
    last_heartbeat = time.time()

    while True:
        try:
            if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL:
                heartbeat(last_seen)
                last_heartbeat = time.time()

            root = fetch_courses()
            courses = get_all_courses(root)
            now = time.strftime("%H:%M:%S")

            # Check for specifically requested courses that are not found
            if not WATCH_REGEX:
                for code in WATCH_COURSES:
                    if code not in courses:
                        if last_seen.get(code) != "NOT_FOUND":
                            print(f"[{now}] {code} : Course not found.")
                            last_seen[code] = "NOT_FOUND"

            # Check for changes in tracked courses
            for code, current in courses.items():
                if WATCH_REGEX:
                    if not WATCH_PATTERN.fullmatch(code):
                        continue
                else:
                    if code not in WATCH_COURSES:
                        continue

                current_vacancies = current["vacancies"]
                previous = last_seen.get(code)

                if previous != current_vacancies:
                    print(
                        f"[{now}] "
                        f"{code} | "
                        f"{current['name']} | "
                        f"{previous} -> {current_vacancies}"
                    )

                    if previous is None and current_vacancies > 0:
                        print(f"✅ {code} is currently available ({current_vacancies} vacancies)\n")
                        notify(code, current["name"], current_vacancies, current["slot"])
                    elif previous == 0 and current_vacancies > 0:
                        print(f"🎉 {code} became available ({current_vacancies} vacancies)\n")
                        notify(code, current["name"], current_vacancies, current["slot"])

                last_seen[code] = current_vacancies

            time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            print("\nStopped.")
            break
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] Error:", e)
            time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    workflow_login(PROXY_PASS)
    monitor()