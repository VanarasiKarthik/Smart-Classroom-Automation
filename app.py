
import os
from openpyxl.utils import get_column_letter
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from io import BytesIO
from flask import make_response, send_file
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from getpass import getpass
from functools import wraps
from flask import (
    Flask, render_template, request, redirect,
    url_for, flash, session
)
from werkzeug.security import generate_password_hash, check_password_hash
import mysql.connector
import os
import re
import subprocess
import sys
import shutil
import secrets
import hashlib
import hmac
import time
import threading
from datetime import datetime, timedelta

app = Flask(__name__)

# Holds the currently running AI attendance process.
attendance_process = None

DB_PASSWORD = os.getenv("DB_PASSWORD") or getpass(
    "Enter MySQL root password: "
)

app.secret_key = os.environ.get("FLASK_SECRET_KEY")

if not app.secret_key:
    app.secret_key = os.urandom(32)


# ---------------- DATABASE CONNECTION ----------------


def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST", "localhost"),
        user=os.getenv("DB_USER", "root"),
        password=DB_PASSWORD,
        database=os.getenv("DB_NAME", "smart_classroom"),
        port=int(os.getenv("DB_PORT", "3306"))
    )



def stop_attendance_process():
    """Stop the AI attendance child process if it is still running."""
    global attendance_process

    if attendance_process is None:
        return False

    try:
        if attendance_process.poll() is None:
            attendance_process.terminate()

            try:
                attendance_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                attendance_process.kill()
                attendance_process.wait(timeout=5)

        return True

    finally:
        attendance_process = None



def complete_expired_sessions():
    """Complete sessions whose scheduled end time has passed."""
    connection = None
    cursor = None
    completed_any = False

    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        cursor.execute("""
            SELECT session_id
            FROM class_sessions
            WHERE status = 'Active'
              AND scheduled_end_at IS NOT NULL
              AND scheduled_end_at <= NOW()
        """)

        expired_sessions = cursor.fetchall()

        for expired_session in expired_sessions:
            cursor.execute("""
                UPDATE class_sessions
                SET status = 'Completed',
                    ended_at = NOW()
                WHERE session_id = %s
                  AND status = 'Active'
                  AND scheduled_end_at <= NOW()
            """, (expired_session["session_id"],))

            if cursor.rowcount == 1:
                completed_any = True
                print(
                    f"Automatically completed class session "
                    f"#{expired_session['session_id']}."
                )

        connection.commit()

        if completed_any:
            stop_attendance_process()

    except Exception as error:
        if connection and connection.is_connected():
            connection.rollback()
        print(f"Session expiry checker error: {error}")

    finally:
        if cursor:
            cursor.close()
        if connection and connection.is_connected():
            connection.close()


def session_expiry_worker():
    """Check the database periodically, even when no lecturer page is open."""
    while True:
        complete_expired_sessions()
        time.sleep(5)


# ---------------- VALIDATION HELPERS ----------------

def valid_mobile_number(mobile_number):
    mobile_number = mobile_number.strip()

    if not re.fullmatch(r"\+?[0-9() -]{7,19}", mobile_number):
        return False

    digit_count = sum(character.isdigit() for character in mobile_number)

    return 7 <= digit_count <= 15


# ---------------- LOGIN PROTECTION ----------------

def login_required(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        if "lecturer_id" not in session:
            flash("Please log in to access the website.")
            return redirect(url_for("login"))

        return function(*args, **kwargs)

    return wrapper

# ---------------- MANAGE LECTURER PROFILE ----------------

@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():

    connection = None
    cursor = None

    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        lecturer_id = session["lecturer_id"]

        if request.method == "POST":

            mobile_number = request.form.get(
                "mobile_number", ""
            ).strip()

            if not valid_mobile_number(mobile_number):
                flash("Please enter a valid mobile number.")

            else:
                try:
                    cursor.execute("""
                        UPDATE lecturers
                        SET mobile_number = %s
                        WHERE lecturer_id = %s
                    """, (
                        mobile_number,
                        lecturer_id
                    ))

                    connection.commit()
                    flash("Mobile number updated successfully!")

                except mysql.connector.IntegrityError:
                    connection.rollback()
                    flash("This mobile number is already registered.")

                return redirect(url_for("profile"))

        cursor.execute("""
            SELECT lecturer_name, username, mobile_number
            FROM lecturers
            WHERE lecturer_id = %s
        """, (lecturer_id,))

        lecturer = cursor.fetchone()

        if not lecturer:
            flash("Lecturer account not found.")
            return redirect(url_for("home"))

        return render_template(
            "profile.html",
            lecturer_name=lecturer["lecturer_name"],
            username=lecturer["username"],
            mobile_number=lecturer["mobile_number"]
        )

    except mysql.connector.Error:
        flash("Unable to load or update your profile.")
        return redirect(url_for("home"))

    finally:
        if cursor:
            cursor.close()

        if connection and connection.is_connected():
            connection.close()


# ---------------- INITIAL LECTURER SETUP ----------------

@app.route("/setup", methods=["GET", "POST"])
def setup():

    connection = get_db_connection()
    cursor = connection.cursor()

    cursor.execute("SELECT COUNT(*) FROM lecturers")
    lecturer_count = cursor.fetchone()[0]

    cursor.close()
    connection.close()

    if lecturer_count > 0:
        return redirect(url_for("login"))

    if request.method == "POST":

        lecturer_name = request.form.get(
            "lecturer_name", ""
        ).strip()

        username = request.form.get(
            "username", ""
        ).strip()

        mobile_number = request.form.get(
            "mobile_number", ""
        ).strip()

        password = request.form.get("password", "")
        confirm_password = request.form.get(
            "confirm_password", ""
        )

        if not lecturer_name or not username or not mobile_number:
            flash("Please fill in all required fields.")
            return redirect(url_for("setup"))

        if not valid_mobile_number(mobile_number):
            flash("Please enter a valid mobile number.")
            return redirect(url_for("setup"))

        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,50}", username):
            flash("Username must be 3-50 characters using letters, numbers, dots, _ or -.")
            return redirect(url_for("setup"))

        if len(password) < 8:
            flash("Password must contain at least 8 characters.")
            return redirect(url_for("setup"))

        if password != confirm_password:
            flash("Passwords do not match.")
            return redirect(url_for("setup"))

        password_hash = generate_password_hash(password)

        connection = None
        cursor = None

        try:
            connection = get_db_connection()
            cursor = connection.cursor()

            cursor.execute("SELECT COUNT(*) FROM lecturers")

            if cursor.fetchone()[0] > 0:
                flash("Initial lecturer account already exists.")
                return redirect(url_for("login"))

            cursor.execute("""
                INSERT INTO lecturers
                (
                    lecturer_name,
                    username,
                    mobile_number,
                    password_hash
                )
                VALUES (%s, %s, %s, %s)
            """, (
                lecturer_name,
                username,
                mobile_number,
                password_hash
            ))

            connection.commit()

            flash("Initial lecturer account created. Please log in.")
            return redirect(url_for("login"))

        except mysql.connector.IntegrityError:
            if connection and connection.is_connected():
                connection.rollback()

            flash("Username or mobile number already exists.")

        except mysql.connector.Error:
            if connection and connection.is_connected():
                connection.rollback()

            flash("Unable to create lecturer account. Check the database.")

        finally:
            if cursor:
                cursor.close()

            if connection and connection.is_connected():
                connection.close()

    return render_template("setup.html")


# ---------------- LECTURER LOGIN ----------------

@app.route("/login", methods=["GET", "POST"])
def login():

    if "lecturer_id" in session:
        return redirect(url_for("home"))

    if request.method == "POST":

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        connection = None
        cursor = None

        try:
            connection = get_db_connection()
            cursor = connection.cursor(dictionary=True)

            cursor.execute("""
                SELECT lecturer_id, lecturer_name,
                       username, password_hash
                FROM lecturers
                WHERE username = %s AND is_active = TRUE
            """, (username,))

            lecturer = cursor.fetchone()

            if lecturer and check_password_hash(
                lecturer["password_hash"], password
            ):
                session.clear()

                session["lecturer_id"] = lecturer["lecturer_id"]
                session["lecturer_name"] = lecturer["lecturer_name"]
                session["username"] = lecturer["username"]

                flash("Login successful!")
                return redirect(url_for("home"))

            flash("Invalid username or password.")

        except mysql.connector.Error:
            flash("Unable to connect to the database.")

        finally:
            if cursor:
                cursor.close()

            if connection and connection.is_connected():
                connection.close()

    return render_template("login.html")


# ---------------- CREATE ADDITIONAL LECTURER ----------------

@app.route("/lecturers/register", methods=["GET", "POST"])
@login_required
def register_lecturer():

    if request.method == "POST":

        lecturer_name = request.form.get(
            "lecturer_name", ""
        ).strip()
        username = request.form.get(
            "username", ""
        ).strip()

        mobile_number = request.form.get(
            "mobile_number", ""
        ).strip()

        password = request.form.get("password", "")
        confirm_password = request.form.get(
            "confirm_password", ""
        )

        if not lecturer_name or not username or not mobile_number:
            flash("Please fill in all required fields.")
            return redirect(url_for("register_lecturer"))

        if not valid_mobile_number(mobile_number):
            flash("Please enter a valid mobile number.")
            return redirect(url_for("register_lecturer"))

        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,50}", username):
            flash("Invalid username format.")
            return redirect(url_for("register_lecturer"))

        if len(password) < 8:
            flash("Password must contain at least 8 characters.")
            return redirect(url_for("register_lecturer"))

        if password != confirm_password:
            flash("Passwords do not match.")
            return redirect(url_for("register_lecturer"))

        password_hash = generate_password_hash(password)

        connection = None
        cursor = None

        try:
            connection = get_db_connection()
            cursor = connection.cursor()

            cursor.execute("""
                INSERT INTO lecturers
                (
                    lecturer_name,
                    username,
                    mobile_number,
                    password_hash
                )
                VALUES (%s, %s, %s, %s)
            """, (
                lecturer_name,
                username,
                mobile_number,
                password_hash
            ))

            connection.commit()
            flash("New lecturer account created successfully!")

        except mysql.connector.IntegrityError:
            if connection and connection.is_connected():
                connection.rollback()

            flash("This username or mobile number already exists.")

        except mysql.connector.Error:
            if connection and connection.is_connected():
                connection.rollback()

            flash("Unable to create lecturer account.")

        finally:
            if cursor:
                cursor.close()

            if connection and connection.is_connected():
                connection.close()

        return redirect(url_for("register_lecturer"))

    return render_template("lecturer_register.html")


# ---------------- FORGOT PASSWORD ----------------

@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():

    if request.method == "POST":

        mobile_number = request.form.get(
            "mobile_number", ""
        ).strip()

        # Clear any previous password recovery attempt.
        session.pop("recovery_mobile", None)
        session.pop("recovery_otp_hash", None)
        session.pop("recovery_otp_salt", None)
        session.pop("recovery_expiry", None)
        session.pop("recovery_attempts", None)

        connection = None
        cursor = None

        try:
            connection = get_db_connection()
            cursor = connection.cursor(dictionary=True)

            cursor.execute("""
                SELECT lecturer_id, mobile_number
                FROM lecturers
                WHERE mobile_number = %s
                  AND is_active = TRUE
            """, (mobile_number,))

            lecturer = cursor.fetchone()

            if lecturer and valid_mobile_number(mobile_number):

                otp = str(secrets.randbelow(900000) + 100000)

                otp_salt = secrets.token_hex(16)

                otp_hash = hashlib.sha256(
                    (otp_salt + otp).encode()
                ).hexdigest()

                session["recovery_mobile"] = mobile_number
                session["recovery_otp_hash"] = otp_hash
                session["recovery_otp_salt"] = otp_salt
                session["recovery_expiry"] = int(time.time()) + 300
                session["recovery_attempts"] = 0

                print("\n===================================")
                print("SMART CLASSROOM PASSWORD RESET")
                print("DEMO OTP:", otp)
                print("This OTP expires in 5 minutes.")
                print("===================================\n")

            flash(
                "If an active lecturer account matches that number, "
                "a password reset OTP has been generated. "
                "Check the Flask terminal for the local demo OTP."
            )

            return redirect(url_for("reset_password"))

        except mysql.connector.Error:
            flash("Unable to process the request. Check the database.")

        finally:
            if cursor:
                cursor.close()

            if connection and connection.is_connected():
                connection.close()

    return render_template("forgot_password.html")


# ---------------- RESET PASSWORD ----------------

@app.route("/reset-password", methods=["GET", "POST"])
def reset_password():

    if "recovery_mobile" not in session:
        if request.method == "GET":
            flash("Please request a new password reset OTP first.")
            return redirect(url_for("forgot_password"))

    if request.method == "POST":

        recovery_mobile = session.get("recovery_mobile")
        stored_hash = session.get("recovery_otp_hash")
        otp_salt = session.get("recovery_otp_salt")
        expiry = session.get("recovery_expiry")
        attempts = session.get("recovery_attempts", 0)

        if not all([
            recovery_mobile,
            stored_hash,
            otp_salt,
            expiry
        ]):
            session.pop("recovery_mobile", None)
            session.pop("recovery_otp_hash", None)
            session.pop("recovery_otp_salt", None)
            session.pop("recovery_expiry", None)
            session.pop("recovery_attempts", None)

            flash("Password reset session expired. Request a new OTP.")
            return redirect(url_for("forgot_password"))

        if time.time() > expiry:
            session.pop("recovery_mobile", None)
            session.pop("recovery_otp_hash", None)
            session.pop("recovery_otp_salt", None)
            session.pop("recovery_expiry", None)
            session.pop("recovery_attempts", None)

            flash("OTP expired. Please request a new OTP.")
            return redirect(url_for("forgot_password"))

        if attempts >= 5:
            session.pop("recovery_mobile", None)
            session.pop("recovery_otp_hash", None)
            session.pop("recovery_otp_salt", None)
            session.pop("recovery_expiry", None)
            session.pop("recovery_attempts", None)

            flash("Maximum OTP attempts reached. Request a new OTP.")
            return redirect(url_for("forgot_password"))

        otp = request.form.get("otp", "").strip()
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get(
            "confirm_password", ""
        )

        if not re.fullmatch(r"[0-9]{6}", otp):
            flash("Please enter a valid 6-digit OTP.")
            return redirect(url_for("reset_password"))

        submitted_hash = hashlib.sha256(
            (otp_salt + otp).encode()
        ).hexdigest()

        if not hmac.compare_digest(submitted_hash, stored_hash):

            session["recovery_attempts"] = attempts + 1

            remaining = 4 - attempts

            if remaining <= 0:
                session.pop("recovery_mobile", None)
                session.pop("recovery_otp_hash", None)
                session.pop("recovery_otp_salt", None)
                session.pop("recovery_expiry", None)
                session.pop("recovery_attempts", None)

                flash("Maximum OTP attempts reached. Request a new OTP.")

            else:
                flash(
                    f"Incorrect OTP. You have {remaining} attempt(s) remaining."
                )

            return redirect(url_for("reset_password"))

        if len(new_password) < 8:
            flash("New password must contain at least 8 characters.")
            return redirect(url_for("reset_password"))

        if new_password != confirm_password:
            flash("Passwords do not match.")
            return redirect(url_for("reset_password"))

        connection = None
        cursor = None

        try:
            connection = get_db_connection()
            cursor = connection.cursor()

            new_password_hash = generate_password_hash(new_password)

            cursor.execute("""
                UPDATE lecturers
                SET password_hash = %s
                WHERE mobile_number = %s
                  AND is_active = TRUE
            """, (
                new_password_hash,
                recovery_mobile
            ))

            if cursor.rowcount == 1:
                connection.commit()

                session.pop("recovery_mobile", None)
                session.pop("recovery_otp_hash", None)
                session.pop("recovery_otp_salt", None)
                session.pop("recovery_expiry", None)
                session.pop("recovery_attempts", None)

                flash("Password reset successful. Please log in.")
                return redirect(url_for("login"))

            connection.rollback()
            flash("Unable to reset password. Request a new OTP.")

        except mysql.connector.Error:
            if connection and connection.is_connected():
                connection.rollback()

            flash("Database error while resetting password.")

        finally:
            if cursor:
                cursor.close()

            if connection and connection.is_connected():
                connection.close()

    return render_template("reset_password.html")


# ---------------- LECTURER LOGOUT ----------------

@app.route("/logout", methods=["POST"])
@login_required
def logout():

    session.clear()
    flash("You have logged out successfully.")

    return redirect(url_for("login"))



# ---------------- HOME DASHBOARD ----------------

@app.route("/", methods=["GET"])
@login_required
def home():

    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    cursor.execute("""
        SELECT COUNT(*) AS total
        FROM students
        WHERE is_active = TRUE
    """)
    total_students = cursor.fetchone()["total"]

    cursor.execute("""
        SELECT COUNT(*) AS total
        FROM subjects
        WHERE is_active = TRUE
    """)
    total_subjects = cursor.fetchone()["total"]
    print("TOTAL ACTIVE SUBJECTS:", total_subjects)

    cursor.execute("""
        SELECT COUNT(DISTINCT a.student_id) AS total
        FROM attendance a
        JOIN students s ON a.student_id = s.student_id
        WHERE DATE(a.attendance_time) = CURDATE()
          AND a.status = 'Present'
          AND s.is_active = TRUE
    """)
    present_today = cursor.fetchone()["total"]

    absent_today = max(0, total_students - present_today)

    cursor.execute("""
        SELECT s.student_name, a.student_id,
               a.attendance_time, a.status
        FROM attendance a
        JOIN students s ON a.student_id = s.student_id
        WHERE DATE(a.attendance_time) = CURDATE()
        ORDER BY a.attendance_time DESC
        LIMIT 10
    """)
    recent_attendance = cursor.fetchall()

    cursor.close()
    connection.close()

    return render_template(
        "index.html",
        total_students=total_students,
        total_subjects=total_subjects,
        present_today=present_today,
        absent_today=absent_today,
        recent_attendance=recent_attendance
    )


# ---------------- STUDENT DETAILS ----------------

@app.route("/students")
@login_required
def students():

    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    cursor.execute("""
        SELECT student_id, student_name, face_folder, created_at
        FROM students
        WHERE is_active = TRUE
        ORDER BY created_at DESC
    """)

    students = cursor.fetchall()

    cursor.close()
    connection.close()

    return render_template("students.html", students=students)


@app.route("/enrollments", methods=["GET", "POST"])
@login_required
def enrollments():

    connection = None
    cursor = None

    if request.method == "POST":

        subject_id = request.form.get("subject_id")
        student_ids = request.form.getlist("student_ids")
        action = request.form.get("action")

        try:
            subject_id = int(subject_id)

            connection = get_db_connection()
            cursor = connection.cursor()

            cursor.execute(
                "SELECT subject_id FROM subjects WHERE subject_id = %s",
                (subject_id,)
            )

            if not cursor.fetchone():
                flash("Selected subject does not exist.")
                return redirect(url_for("enrollments"))

            if action == "enroll":

                for student_id in student_ids:
                    cursor.execute("""
                        INSERT IGNORE INTO student_subjects
                        (student_id, subject_id)
                        SELECT student_id, %s
                        FROM students
                        WHERE student_id = %s AND is_active = TRUE
                    """, (subject_id, student_id))

                connection.commit()
                flash("Student enrollment updated successfully!")

            elif action == "remove":

                for student_id in student_ids:
                    cursor.execute("""
                        DELETE FROM student_subjects
                        WHERE student_id = %s AND subject_id = %s
                    """, (student_id, subject_id))

                connection.commit()
                flash("Selected students removed from this subject.")

        except (ValueError, mysql.connector.Error):
            if connection and connection.is_connected():
                connection.rollback()

            flash("Unable to update student enrollment.")

        finally:
            if cursor:
                cursor.close()

            if connection and connection.is_connected():
                connection.close()

        return redirect(url_for("enrollments", subject_id=subject_id))

    subjects_list = []
    student_list = []
    selected_subject = request.args.get("subject_id", "")

    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        cursor.execute("""
            SELECT subject_id, subject_name
            FROM subjects
            WHERE is_active = TRUE
            ORDER BY subject_name
        """)

        subjects_list = cursor.fetchall()

        if selected_subject:
            cursor.execute("""
                SELECT
                    s.student_id,
                    s.student_name,
                    CASE
                        WHEN ss.student_id IS NOT NULL THEN 1
                        ELSE 0
                    END AS enrolled
                FROM students s
                LEFT JOIN student_subjects ss
                    ON s.student_id = ss.student_id
                    AND ss.subject_id = %s
                WHERE s.is_active = TRUE
                ORDER BY s.student_name
            """, (int(selected_subject),))

            student_list = cursor.fetchall()

    except (ValueError, mysql.connector.Error):
        flash("Unable to load enrollment information.")

    finally:
        if cursor:
            cursor.close()

        if connection and connection.is_connected():
            connection.close()

    return render_template(
        "enrollments.html",
        subjects=subjects_list,
        students=student_list,
        selected_subject=selected_subject
    )



# ---------------- STUDENT REGISTRATION ----------------

@app.route("/register", methods=["POST"])
@login_required
def register_student():

    student_id = request.form.get("student_id", "").strip()
    student_name = request.form.get("student_name", "").strip()

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,30}", student_id):
        flash("Invalid Student ID. Use letters, numbers, _ or -.")
        return redirect(url_for("home"))

    if not student_name:
        flash("Please enter the student's name.")
        return redirect(url_for("home"))

    face_folder = os.path.join("dataset", student_id)
    folder_path = os.path.join(app.root_path, face_folder)

    connection = None
    cursor = None

    try:
        os.makedirs(folder_path, exist_ok=True)

        connection = get_db_connection()
        cursor = connection.cursor()

        cursor.execute("""
            INSERT INTO students (student_id, student_name, face_folder)
            VALUES (%s, %s, %s)
        """, (student_id, student_name, face_folder))

        connection.commit()
        flash("Student registered successfully!")

    except mysql.connector.IntegrityError:
        flash("This Student ID already exists. Check whether it is inactive.")

    except mysql.connector.Error:
        flash("Database error. Please check the MySQL connection.")

    finally:
        if cursor:
            cursor.close()

        if connection and connection.is_connected():
            connection.close()

    return redirect(url_for("home"))


# ---------------- FACE REGISTRATION ----------------

@app.route("/register-face", methods=["POST"])
@login_required
def register_face():

    student_id = request.form.get("student_id", "").strip()

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,30}", student_id):
        flash("Invalid Student ID.")
        return redirect(url_for("students"))

    connection = None
    cursor = None

    try:
        connection = get_db_connection()
        cursor = connection.cursor()

        cursor.execute("""
            SELECT student_id
            FROM students
            WHERE student_id = %s AND is_active = TRUE
        """, (student_id,))

        student = cursor.fetchone()

    except mysql.connector.Error:
        flash("Unable to verify student in MySQL.")
        return redirect(url_for("students"))

    finally:
        if cursor:
            cursor.close()

        if connection and connection.is_connected():
            connection.close()

    if not student:
        flash("Active student not found.")
        return redirect(url_for("students"))

    script_path = os.path.join(
        app.root_path,
        "face_registration.py"
    )

    if not os.path.isfile(script_path):
        flash("Face registration script not found.")
        return redirect(url_for("students"))

    try:
        result = subprocess.run(
            [sys.executable, script_path, student_id],
            cwd=app.root_path,
            check=False
        )

        if result.returncode == 0:
            flash(f"Face registration finished for {student_id}.")
        else:
            flash(f"Face registration failed for {student_id}. Check Flask terminal.")

    except Exception as error:
        flash(f"Face registration error: {error}")

    return redirect(url_for("students"))


# ---------------- DEACTIVATE STUDENT ----------------

@app.route("/deactivate-student", methods=["POST"])
@login_required
def deactivate_student():

    student_id = request.form.get("student_id", "").strip()

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,30}", student_id):
        flash("Invalid Student ID.")
        return redirect(url_for("students"))

    connection = None
    cursor = None

    try:
        connection = get_db_connection()
        cursor = connection.cursor()

        cursor.execute("""
            SELECT student_id, is_active
            FROM students
            WHERE student_id = %s
        """, (student_id,))

        student = cursor.fetchone()

        if not student:
            flash("Student not found.")
            return redirect(url_for("students"))

        if not student[1]:
            flash("Student is already inactive.")
            return redirect(url_for("students"))

        cursor.execute("""
            UPDATE students
            SET is_active = FALSE
            WHERE student_id = %s
        """, (student_id,))

        connection.commit()

    except mysql.connector.Error:
        if connection and connection.is_connected():
            connection.rollback()

        flash("Unable to deactivate student. Check MySQL connection.")
        return redirect(url_for("students"))

    finally:
        if cursor:
            cursor.close()

        if connection and connection.is_connected():
            connection.close()

    dataset_root = os.path.realpath(
        os.path.join(app.root_path, "dataset")
    )

    student_folder = os.path.realpath(
        os.path.join(dataset_root, student_id)
    )

    if os.path.commonpath([dataset_root, student_folder]) != dataset_root:
        flash("Student deactivated, but dataset path was not valid.")
        return redirect(url_for("students"))

    try:
        if os.path.isdir(student_folder):
            shutil.rmtree(student_folder)

        flash(
            f"Student {student_id} deactivated. "
            "Attendance history has been preserved."
        )

    except OSError:
        flash(
            f"Student {student_id} deactivated, but face images "
            "could not be deleted. Check the dataset folder."
        )

    return redirect(url_for("students"))


# ---------------- SUBJECT MANAGEMENT ----------------

@app.route("/subjects", methods=["GET", "POST"])
@login_required
def subjects():

    if request.method == "POST":

        subject_name = request.form.get("subject_name", "").strip()
        subject_code = request.form.get("subject_code", "").strip()

        if not subject_name:
            flash("Please enter a subject name.")
            return redirect(url_for("subjects"))

        if len(subject_name) > 120:
            flash("Subject name must not exceed 120 characters.")
            return redirect(url_for("subjects"))

        if len(subject_code) > 30:
            flash("Subject code must not exceed 30 characters.")
            return redirect(url_for("subjects"))

        if subject_code == "":
            subject_code = None

        connection = None
        cursor = None

        try:
            connection = get_db_connection()
            cursor = connection.cursor()

            cursor.execute("""
                INSERT INTO subjects
                (subject_name, subject_code, created_by)
                VALUES (%s, %s, %s)
            """, (
                subject_name,
                subject_code,
                session["lecturer_id"]
            ))

            connection.commit()
            flash("Subject added successfully!")

        except mysql.connector.IntegrityError:
            if connection and connection.is_connected():
                connection.rollback()

            flash("This subject name or subject code already exists.")

        except mysql.connector.Error:
            if connection and connection.is_connected():
                connection.rollback()

            flash("Unable to add subject. Check the database connection.")

        finally:
            if cursor:
                cursor.close()

            if connection and connection.is_connected():
                connection.close()

        return redirect(url_for("subjects"))

    connection = None
    cursor = None
    subject_list = []

    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        cursor.execute("""
            SELECT
                s.subject_id,
                s.subject_name,
                s.subject_code,
                s.is_active,
                s.created_at,
                l.lecturer_name
            FROM subjects s
            LEFT JOIN lecturers l
                ON s.created_by = l.lecturer_id
            ORDER BY s.created_at DESC
        """)

        subject_list = cursor.fetchall()

    except mysql.connector.Error:
        flash("Unable to load subjects from the database.")

    finally:
        if cursor:
            cursor.close()

        if connection and connection.is_connected():
            connection.close()

    return render_template(
        "subjects.html",
        subjects=subject_list
    )


# ---------------- DEACTIVATE SUBJECT ----------------

@app.route("/deactivate-subject", methods=["POST"])
@login_required
def deactivate_subject():

    subject_id = request.form.get("subject_id", "").strip()

    if not subject_id.isdigit():
        flash("Invalid subject ID.")
        return redirect(url_for("subjects"))

    connection = None
    cursor = None

    try:
        connection = get_db_connection()
        cursor = connection.cursor()

        cursor.execute("""
            UPDATE subjects
            SET is_active = FALSE
            WHERE subject_id = %s AND is_active = TRUE
        """, (int(subject_id),))

        connection.commit()

        if cursor.rowcount > 0:
            flash("Subject deactivated successfully. Its record has been preserved.")
        else:
            flash("Subject not found or it is already inactive.")

    except mysql.connector.Error:
        if connection and connection.is_connected():
            connection.rollback()

        flash("Unable to deactivate subject. Check the database connection.")

    finally:
        if cursor:
            cursor.close()

        if connection and connection.is_connected():
            connection.close()

    return redirect(url_for("subjects"))


# ---------------- REACTIVATE SUBJECT ----------------

@app.route("/reactivate-subject", methods=["POST"])
@login_required
def reactivate_subject():

    subject_id = request.form.get("subject_id", "").strip()

    if not subject_id.isdigit():
        flash("Invalid subject ID.")
        return redirect(url_for("subjects"))

    connection = None
    cursor = None

    try:
        connection = get_db_connection()
        cursor = connection.cursor()

        cursor.execute("""
            UPDATE subjects
            SET is_active = TRUE
            WHERE subject_id = %s
              AND is_active = FALSE
        """, (int(subject_id),))

        connection.commit()

        if cursor.rowcount == 1:
            flash("Subject reactivated successfully!")
        else:
            flash("Subject not found or it is already active.")

    except mysql.connector.Error:
        if connection and connection.is_connected():
            connection.rollback()

        flash("Unable to reactivate subject. Check the database connection.")

    finally:
        if cursor:
            cursor.close()

        if connection and connection.is_connected():
            connection.close()

    return redirect(url_for("subjects"))


# ---------------- CLASS SESSION MANAGEMENT ----------------

@app.route("/sessions", methods=["GET", "POST"])
@login_required
def manage_sessions():

    connection = None
    cursor = None

    if request.method == "POST":

        action = request.form.get("action", "").strip()

        # END AN ACTIVE CLASS SESSION
        if action == "end_session":

            session_id = request.form.get("session_id", "").strip()

            if not session_id.isdigit():
                flash("Invalid class session ID.")
                return redirect(url_for("manage_sessions"))

            try:
                connection = get_db_connection()
                cursor = connection.cursor()

                cursor.execute("""
                    UPDATE class_sessions
                    SET status = 'Completed',
                        ended_at = NOW()
                    WHERE session_id = %s
                      AND lecturer_id = %s
                      AND status = 'Active'
                """, (
                    int(session_id),
                    session["lecturer_id"]
                ))

                if cursor.rowcount == 1:
                    connection.commit()

                    # Stop webcam attendance when its class session ends.
                    stop_attendance_process()

                    flash(f"Class session #{session_id} ended successfully.")
                else:
                    connection.rollback()
                    flash(
                        "Unable to end this session. "
                        "It may already be completed or belong to another lecturer."
                    )

            except mysql.connector.Error:
                if connection and connection.is_connected():
                    connection.rollback()

                flash("Unable to end the class session. Check the database.")

            finally:
                if cursor:
                    cursor.close()

                if connection and connection.is_connected():
                    connection.close()

            return redirect(url_for("manage_sessions"))

        # START A NEW CLASS SESSION
        subject_id = request.form.get("subject_id", "").strip()
        duration_mode = request.form.get("duration_mode", "normal").strip()

        if not subject_id.isdigit():
            flash("Please select a valid subject.")
            return redirect(url_for("manage_sessions"))

        if duration_mode == "demo":
            duration_value = request.form.get("duration_minutes", "").strip()

            if duration_value not in {"2", "5", "10"}:
                flash("Please select a Demo Mode duration of 2, 5, or 10 minutes.")
                return redirect(url_for("manage_sessions"))

            duration_minutes = int(duration_value)
            duration_hours = None
            duration_label = f"{duration_minutes} minute(s)"

        elif duration_mode == "normal":
            duration_value = request.form.get("duration_hours", "").strip()

            if duration_value not in {"1", "2", "3", "4"}:
                flash("Please select a Normal Mode duration between 1 and 4 hours.")
                return redirect(url_for("manage_sessions"))

            duration_hours = int(duration_value)
            duration_minutes = duration_hours * 60
            duration_label = f"{duration_hours} hour(s)"

        else:
            flash("Please select a valid session mode.")
            return redirect(url_for("manage_sessions"))

        try:
            connection = get_db_connection()
            cursor = connection.cursor(dictionary=True)

            cursor.execute("""
                SELECT session_id
                FROM class_sessions
                WHERE status = 'Active'
                LIMIT 1
            """)

            active_session = cursor.fetchone()

            if active_session:
                flash(
                    f"Session #{active_session['session_id']} is already active. "
                    "End that session before starting another."
                )
                return redirect(url_for("manage_sessions"))

            cursor.execute("""
                SELECT subject_id, subject_name
                FROM subjects
                WHERE subject_id = %s AND is_active = TRUE
            """, (int(subject_id),))

            subject = cursor.fetchone()

            if not subject:
                flash("The selected subject is unavailable or inactive.")
                return redirect(url_for("manage_sessions"))

            started_at = datetime.now().replace(microsecond=0)
            scheduled_end_at = started_at + timedelta(minutes=duration_minutes)

            cursor.execute("""
                INSERT INTO class_sessions
                (
                    subject_name,
                    session_date,
                    start_time,
                    subject_id,
                    lecturer_id,
                    duration_hours,
                    duration_minutes,
                    started_at,
                    scheduled_end_at,
                    status
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'Active')
            """, (
                subject["subject_name"],
                started_at.date(),
                started_at.time(),
                subject["subject_id"],
                session["lecturer_id"],
                duration_hours,
                duration_minutes,
                started_at,
                scheduled_end_at
            ))

            new_session_id = cursor.lastrowid
            connection.commit()

            flash(
                f"Class session #{new_session_id} started for "
                f"{subject['subject_name']} ({duration_label})."
            )

        except mysql.connector.Error:
            if connection and connection.is_connected():
                connection.rollback()

            flash("Unable to start the class session. Check the database connection.")

        finally:
            if cursor:
                cursor.close()

            if connection and connection.is_connected():
                connection.close()

        return redirect(url_for("manage_sessions"))

    # LOAD SESSION PAGE
    active_subjects = []
    active_session = None

    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        cursor.execute("""
            SELECT subject_id, subject_name, subject_code
            FROM subjects
            WHERE is_active = TRUE
            ORDER BY subject_name
        """)

        active_subjects = cursor.fetchall()

        cursor.execute("""
            SELECT
                cs.session_id,
                cs.subject_name,
                cs.duration_hours,
                cs.duration_minutes,
                cs.started_at,
                cs.scheduled_end_at,
                l.lecturer_name
            FROM class_sessions cs
            LEFT JOIN lecturers l
                ON cs.lecturer_id = l.lecturer_id
            WHERE cs.status = 'Active'
            ORDER BY cs.started_at DESC
            LIMIT 1
        """)

        active_session = cursor.fetchone()

    except mysql.connector.Error:
        flash("Unable to load class sessions. Check the database connection.")

    finally:
        if cursor:
            cursor.close()

        if connection and connection.is_connected():
            connection.close()

    attendance_running = (
        attendance_process is not None
        and attendance_process.poll() is None
    )

    return render_template(
        "sessions.html",
        subjects=active_subjects,
        active_session=active_session,
        attendance_running=attendance_running
    )



@app.route("/attendance-management")
@login_required
def attendance_management():

    connection = None
    cursor = None
    completed_sessions = []

    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        cursor.execute("""
            SELECT
                cs.session_id,
                cs.subject_name,
                cs.session_date,
                cs.started_at,
                cs.ended_at,
                l.lecturer_name,

                COALESCE(
                    cs.duration_minutes,
                    TIMESTAMPDIFF(
                        MINUTE,
                        cs.started_at,
                        COALESCE(cs.ended_at, cs.scheduled_end_at)
                    )
                ) AS duration_minutes,

                (
                    SELECT COUNT(*)
                    FROM student_subjects ss
                    WHERE ss.subject_id = cs.subject_id
                ) AS total_enrolled,

                (
                    SELECT COUNT(DISTINCT a.student_id)
                    FROM attendance a
                    INNER JOIN student_subjects ss
                        ON ss.student_id = a.student_id
                        AND ss.subject_id = cs.subject_id
                    WHERE a.session_id = cs.session_id
                      AND a.status = 'Present'
                ) AS present_count

            FROM class_sessions cs

            LEFT JOIN lecturers l
                ON cs.lecturer_id = l.lecturer_id

            WHERE cs.status = 'Completed'

            ORDER BY cs.session_date DESC, cs.session_id DESC
        """)

        completed_sessions = cursor.fetchall()

        for item in completed_sessions:
            item["absent_count"] = (
                item["total_enrolled"] - item["present_count"]
            )

            item["attendance_percentage"] = (
                round(
                    item["present_count"] / item["total_enrolled"] * 100,
                    2
                )
                if item["total_enrolled"] > 0 else 0
            )

    except mysql.connector.Error:
        flash("Unable to load attendance management records.")

    finally:
        if cursor:
            cursor.close()

        if connection and connection.is_connected():
            connection.close()

    return render_template(
        "attendance_management.html",
        sessions=completed_sessions
    )


@app.route("/attendance-report/<int:session_id>")
@login_required
def attendance_report(session_id):

    connection = None
    cursor = None
    session_info = None
    student_records = []

    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        # Get completed session information
        cursor.execute("""
            SELECT
                cs.session_id,
                cs.subject_name,
                cs.session_date,
                cs.started_at,
                cs.ended_at,
                cs.duration_minutes,
                l.lecturer_name
            FROM class_sessions cs
            LEFT JOIN lecturers l
                ON cs.lecturer_id = l.lecturer_id
            WHERE cs.session_id = %s
              AND cs.status = 'Completed'
        """, (session_id,))

        session_info = cursor.fetchone()

        if not session_info:
            flash("Completed class session not found.")
            return redirect(url_for("attendance_management"))

        # Get every enrolled student and their attendance
        cursor.execute("""
            SELECT
                s.student_id,
                s.student_name,
                COALESCE(a.status, 'Absent') AS attendance_status,
                a.attendance_time
            FROM student_subjects ss
            INNER JOIN students s
                ON ss.student_id = s.student_id
            LEFT JOIN attendance a
                ON a.student_id = ss.student_id
                AND a.session_id = %s
            WHERE ss.subject_id = (
                SELECT subject_id
                FROM class_sessions
                WHERE session_id = %s
            )
            ORDER BY s.student_id
        """, (session_id, session_id))

        student_records = cursor.fetchall()

        present_count = sum(
            1 for student in student_records
            if student["attendance_status"] == "Present"
        )

        total_enrolled = len(student_records)
        absent_count = total_enrolled - present_count

        attendance_percentage = (
            round(present_count / total_enrolled * 100, 2)
            if total_enrolled > 0 else 0
        )

    except mysql.connector.Error:
        flash("Unable to generate the individual attendance report.")
        return redirect(url_for("attendance_management"))

    finally:
        if cursor:
            cursor.close()

        if connection and connection.is_connected():
            connection.close()

    return render_template(
        "attendance_report.html",
        session_info=session_info,
        students=student_records,
        total_enrolled=total_enrolled,
        present_count=present_count,
        absent_count=absent_count,
        attendance_percentage=attendance_percentage
    )

# ---------------- AI ATTENDANCE CONTROL ----------------

@app.route("/start-attendance", methods=["POST"])
@login_required
def start_attendance():
    global attendance_process

    connection = None
    cursor = None

    # Do not launch a second webcam process.
    if attendance_process is not None:
        if attendance_process.poll() is None:
            flash("AI attendance is already running.")
            return redirect(url_for("manage_sessions"))

        attendance_process = None

    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        # Only allow the logged-in lecturer to start attendance
        # for their own active class session.
        cursor.execute(
            """
            SELECT session_id, subject_name
            FROM class_sessions
            WHERE status = 'Active'
              AND lecturer_id = %s
            ORDER BY started_at DESC, session_id DESC
            LIMIT 1
            """,
            (session["lecturer_id"],)
        )

        active_class = cursor.fetchone()

        if not active_class:
            flash("Please start your class session before starting AI attendance.")
            return redirect(url_for("manage_sessions"))

        script_path = os.path.join(
            app.root_path,
            "face_recognition.py"
        )

        if not os.path.isfile(script_path):
            flash("face_recognition.py was not found in the project folder.")
            return redirect(url_for("manage_sessions"))

        child_environment = os.environ.copy()
        child_environment["SMART_CLASSROOM_DB_PASSWORD"] = DB_PASSWORD
        child_environment["SMART_CLASSROOM_SESSION_ID"] = str(
            active_class["session_id"]
        )

        attendance_process = subprocess.Popen(
            [sys.executable, script_path],
            cwd=app.root_path,
            env=child_environment
        )

        flash(
            f"AI attendance launched for {active_class['subject_name']} "
            f"(Session #{active_class['session_id']}). "
            "The webcam window should open shortly."
        )

    except Exception as error:
        attendance_process = None
        flash(f"Unable to start AI attendance: {error}")

    finally:
        if cursor:
            cursor.close()

        if connection and connection.is_connected():
            connection.close()

    return redirect(url_for("manage_sessions"))


@app.route("/stop-attendance", methods=["POST"])
@login_required
def stop_attendance():
    global attendance_process

    if attendance_process is None:
        flash("AI attendance is not currently running.")
        return redirect(url_for("manage_sessions"))

    try:
        was_running = attendance_process.poll() is None
        stop_attendance_process()

        if was_running:
            flash("AI attendance stopped. The webcam has been released.")
        else:
            flash("The AI attendance program had already stopped.")

    except Exception as error:
        attendance_process = None
        flash(f"Unable to stop AI attendance: {error}")

    return redirect(url_for("manage_sessions"))


@app.route("/download-attendance-pdf/<int:session_id>")
@login_required
def download_attendance_pdf(session_id):

    connection = None
    cursor = None

    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        cursor.execute("""
            SELECT
                cs.session_id,
                cs.subject_id,
                cs.subject_name,
                cs.session_date,
                cs.started_at,
                cs.ended_at,
                cs.duration_minutes,
                l.lecturer_name
            FROM class_sessions cs
            LEFT JOIN lecturers l
                ON cs.lecturer_id = l.lecturer_id
            WHERE cs.session_id = %s
              AND cs.status = 'Completed'
        """, (session_id,))

        session_info = cursor.fetchone()

        if not session_info:
            flash("Completed class session not found.")
            return redirect(url_for("attendance_management"))

        cursor.execute("""
            SELECT
                s.student_id,
                s.student_name,
                COALESCE(a.status, 'Absent') AS attendance_status,
                a.attendance_time
            FROM student_subjects ss
            INNER JOIN students s
                ON ss.student_id = s.student_id
            LEFT JOIN attendance a
                ON a.student_id = ss.student_id
                AND a.session_id = %s
            WHERE ss.subject_id = %s
            ORDER BY s.student_id
        """, (session_id, session_info["subject_id"]))

        students = cursor.fetchall()

        total_enrolled = len(students)
        present_count = sum(
            1 for student in students
            if student["attendance_status"] == "Present"
        )
        absent_count = total_enrolled - present_count

        attendance_percentage = (
            round(present_count / total_enrolled * 100, 2)
            if total_enrolled else 0
        )

        pdf_buffer = BytesIO()

        document = SimpleDocTemplate(
            pdf_buffer,
            pagesize=landscape(A4)
        )

        styles = getSampleStyleSheet()
        content = []

        content.append(Paragraph(
            "CLASS ATTENDANCE REPORT",
            styles["Title"]
        ))
        content.append(Spacer(1, 15))

        content.append(Paragraph(
            f"<b>Session ID:</b> {session_info['session_id']}<br/>"
            f"<b>Subject:</b> {session_info['subject_name']}<br/>"
            f"<b>Lecturer:</b> {session_info['lecturer_name'] or 'N/A'}<br/>"
            f"<b>Session Date:</b> {session_info['session_date']}<br/>"
            f"<b>Start Time:</b> {session_info['started_at'] or 'N/A'}<br/>"
            f"<b>End Time:</b> {session_info['ended_at'] or 'N/A'}<br/>"
            f"<b>Duration:</b> {session_info['duration_minutes'] or 0} minutes",
            styles["Normal"]
        ))

        content.append(Spacer(1, 15))

        summary_data = [
            ["Total Enrolled", "Present", "Absent", "Attendance"],
            [
                str(total_enrolled),
                str(present_count),
                str(absent_count),
                f"{attendance_percentage}%"
            ]
        ]

        summary_table = Table(summary_data)
        summary_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.darkblue),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 1, colors.black),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("PADDING", (0, 0), (-1, -1), 8)
        ]))

        content.append(summary_table)
        content.append(Spacer(1, 20))

        student_data = [[
            "Student ID",
            "Student Name",
            "Attendance Status",
            "Attendance Time"
        ]]

        for student in students:
            student_data.append([
                student["student_id"],
                student["student_name"],
                student["attendance_status"],
                str(student["attendance_time"] or "N/A")
            ])

        if not students:
            student_data.append(["-", "No enrolled students", "-", "-"])

        student_table = Table(
            student_data,
            colWidths=[120, 180, 150, 180],
            repeatRows=1
        )

        student_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.darkblue),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 1, colors.black),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("PADDING", (0, 0), (-1, -1), 8),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE")
        ]))

        content.append(student_table)

        document.build(content)

        pdf_buffer.seek(0)

        return send_file(
            pdf_buffer,
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"attendance_report_session_{session_id}.pdf"
        )

    except Exception:
        flash("Unable to generate PDF attendance report.")
        return redirect(url_for("attendance_management"))

    finally:
        if cursor:
            cursor.close()

        if connection and connection.is_connected():
            connection.close()



@app.route("/download-attendance-excel/<int:session_id>")
@login_required
def download_attendance_excel(session_id):

    connection = None
    cursor = None

    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        cursor.execute("""
            SELECT
                cs.session_id,
                cs.subject_id,
                cs.subject_name,
                cs.session_date,
                cs.started_at,
                cs.ended_at,
                cs.duration_minutes,
                l.lecturer_name
            FROM class_sessions cs
            LEFT JOIN lecturers l
                ON cs.lecturer_id = l.lecturer_id
            WHERE cs.session_id = %s
              AND cs.status = 'Completed'
        """, (session_id,))

        session_info = cursor.fetchone()

        if not session_info:
            flash("Completed class session not found.")
            return redirect(url_for("attendance_management"))

        cursor.execute("""
            SELECT
                s.student_id,
                s.student_name,
                COALESCE(a.status, 'Absent') AS attendance_status,
                a.attendance_time
            FROM student_subjects ss
            INNER JOIN students s
                ON ss.student_id = s.student_id
            LEFT JOIN attendance a
                ON a.student_id = ss.student_id
                AND a.session_id = %s
            WHERE ss.subject_id = %s
            ORDER BY s.student_id
        """, (session_id, session_info["subject_id"]))

        students = cursor.fetchall()

        total_enrolled = len(students)
        present_count = sum(
            1 for student in students
            if student["attendance_status"] == "Present"
        )
        absent_count = total_enrolled - present_count

        attendance_percentage = (
            round(present_count / total_enrolled * 100, 2)
            if total_enrolled else 0
        )

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Attendance Report"

        sheet.merge_cells("A1:D1")
        sheet["A1"] = "CLASS ATTENDANCE REPORT"
        sheet["A1"].font = Font(
            bold=True,
            size=16,
            color="FFFFFF"
        )
        sheet["A1"].fill = PatternFill(
            "solid",
            fgColor="243B55"
        )
        sheet["A1"].alignment = Alignment(horizontal="center")

        sheet.append([])
        sheet.append(["Session ID", session_info["session_id"]])
        sheet.append(["Subject", session_info["subject_name"]])
        sheet.append(["Lecturer", session_info["lecturer_name"] or "N/A"])
        sheet.append(["Session Date", str(session_info["session_date"] or "N/A")])
        sheet.append(["Start Time", str(session_info["started_at"] or "N/A")])
        sheet.append(["End Time", str(session_info["ended_at"] or "N/A")])
        sheet.append(["Duration (Minutes)", session_info["duration_minutes"] or 0])

        sheet.append([])
        sheet.append(["Total Enrolled", "Present", "Absent", "Attendance %"])
        sheet.append([
            total_enrolled,
            present_count,
            absent_count,
            attendance_percentage
        ])

        sheet.append([])
        sheet.append([
            "S.No",
            "Student ID",
            "Student Name",
            "Attendance Status",
            "Attendance Time"
        ])

        header_row = sheet.max_row

        for cell in sheet[header_row]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill(
                "solid",
                fgColor="243B55"
            )
            cell.alignment = Alignment(horizontal="center")

        for index, student in enumerate(students, start=1):
            sheet.append([
                index,
                student["student_id"],
                student["student_name"],
                student["attendance_status"],
                str(student["attendance_time"] or "Not Marked")
            ])

        if not students:
            sheet.append(["-", "-", "No enrolled students", "-", "-"])

            for column_index, column_cells in enumerate(sheet.columns, start=1):
                column_letter = get_column_letter(column_index)

            max_length = max(
                len(str(cell.value or ""))
                for cell in column_cells
            )

            sheet.column_dimensions[column_letter].width = min(
                max_length + 4, 35
            )

        excel_buffer = BytesIO()
        workbook.save(excel_buffer)
        excel_buffer.seek(0)

        return send_file(
            excel_buffer,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=f"attendance_report_session_{session_id}.xlsx"
        )

    except Exception as error:
        print("Excel report error:", error)
        flash("Unable to generate Excel attendance report.")
        return redirect(url_for("attendance_management"))

    finally:
        if cursor:
            cursor.close()

        if connection and connection.is_connected():
            connection.close()


# ---------------- RUN FLASK ----------------

if __name__ == "__main__":

    expiry_thread = threading.Thread(
        target=session_expiry_worker,
        daemon=True
    )
    expiry_thread.start()

    app.run(
        debug=True,
        use_reloader=False
    )