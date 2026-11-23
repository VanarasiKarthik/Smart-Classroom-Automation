import cv2
import os
import glob
import numpy as np
import mysql.connector
from getpass import getpass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DETECTOR_MODEL = os.path.join(
    BASE_DIR, "face_detection_yunet_2023mar.onnx"
)

RECOGNIZER_MODEL = os.path.join(
    BASE_DIR, "face_recognition_sface_2021dec.onnx"
)

DATASET_PATH = os.path.join(BASE_DIR, "dataset")

THRESHOLD = 0.363

connection = None
cursor = None
camera = None


def load_student_features(detector, recognizer, students):
    student_templates = {}

    for student in students:
        student_id = student["student_id"]
        student_name = student["student_name"]

        student_folder = os.path.join(DATASET_PATH, student_id)
        image_paths = glob.glob(os.path.join(student_folder, "*.jpg"))

        if not image_paths:
            print(f"Skipping {student_id}: No face images found.")
            continue

        features = []

        for image_path in image_paths:
            image = cv2.imread(image_path)
            if image is None:
                continue

            detector.setInputSize((image.shape[1], image.shape[0]))
            _, faces = detector.detect(image)

            if faces is None or len(faces) == 0:
                continue

            face = max(faces, key=lambda f: f[2] * f[3])
            aligned_face = recognizer.alignCrop(image, face)
            feature = recognizer.feature(aligned_face)
            features.append(feature.flatten())

        if not features:
            print(f"Skipping {student_id}: No usable face samples.")
            continue

        template = np.mean(features, axis=0)
        norm = np.linalg.norm(template)
        if norm == 0:
            continue

        template = (template / norm).reshape(1, -1).astype(np.float32)
        student_templates[student_id] = {
            "name": student_name,
            "template": template
        }

        print(
            f"Loaded: {student_id} - {student_name} "
            f"({len(features)} samples)"
        )

    return student_templates


try:
    # Flask supplies these environment variables when it launches this file.
    # When run manually, the MySQL password is requested in the terminal.
    db_password = (
        os.environ.get("SMART_CLASSROOM_DB_PASSWORD")
        or getpass("Enter MySQL root password: ")
    )

    connection = mysql.connector.connect(
        host="localhost",
        user="root",
        password=db_password,
        database="smart_classroom"
    )
    cursor = connection.cursor(dictionary=True)

    cursor.execute(
        """
        SELECT student_id, student_name
        FROM students
        WHERE is_active = TRUE
        ORDER BY student_id
        """
    )
    students = cursor.fetchall()

    if not students:
        raise RuntimeError("No active students found in MySQL.")

    requested_session_id = os.environ.get("SMART_CLASSROOM_SESSION_ID")

    if requested_session_id:
        if not requested_session_id.isdigit():
            raise RuntimeError("Invalid classroom session ID.")

        cursor.execute(
            """
            SELECT session_id, subject_name
            FROM class_sessions
            WHERE session_id = %s AND status = 'Active'
            LIMIT 1
            """,
            (int(requested_session_id),)
        )
    else:
        cursor.execute(
            """
            SELECT session_id, subject_name
            FROM class_sessions
            WHERE status = 'Active'
            ORDER BY started_at DESC, session_id DESC
            LIMIT 1
            """
        )

    active_session = cursor.fetchone()
    if active_session is None:
        raise RuntimeError(
            "The selected class session is not active. "
            "Please start an active session from the website."
        )

    SESSION_ID = active_session["session_id"]
    SUBJECT_NAME = active_session["subject_name"]

    print("\nMySQL connected successfully!")
    print("Active students:", len(students))
    print("Active Session ID:", SESSION_ID)
    print("Subject:", SUBJECT_NAME)

    if not os.path.isfile(DETECTOR_MODEL):
        raise FileNotFoundError(f"YuNet model not found: {DETECTOR_MODEL}")
    if not os.path.isfile(RECOGNIZER_MODEL):
        raise FileNotFoundError(f"SFace model not found: {RECOGNIZER_MODEL}")

    detector = cv2.FaceDetectorYN.create(
        DETECTOR_MODEL,
        "",
        (320, 320),
        0.8,
        0.3,
        5000
    )
    recognizer = cv2.FaceRecognizerSF.create(RECOGNIZER_MODEL, "")

    student_templates = load_student_features(detector, recognizer, students)
    if not student_templates:
        raise RuntimeError("No usable active student face datasets found.")

    print("\nFace datasets loaded:", len(student_templates))

    cursor.execute(
        """
        SELECT student_id
        FROM attendance
        WHERE session_id = %s
        """,
        (SESSION_ID,)
    )
    marked_students = {row["student_id"] for row in cursor.fetchall()}

    print("Starting automatic attendance...")
    print("Press Q to close the camera.\n")

    camera_index = int(os.environ.get("SMART_CLASSROOM_CAMERA_INDEX", "0"))
    camera = cv2.VideoCapture(camera_index, cv2.CAP_DSHOW)

    if not camera.isOpened():
        camera.release()
        camera = cv2.VideoCapture(camera_index)

    if not camera.isOpened():
        raise RuntimeError(
            f"Camera {camera_index} could not be opened. "
            "Check Windows camera permissions and close other camera apps."
        )

    attendance_message = "WAITING FOR RECOGNITION"
    window_name = "AI Smart Classroom - Multi Student Attendance"

    while True:
        ret, frame = camera.read()
        if not ret:
            print("Could not read camera frame.")
            break

        detector.setInputSize((frame.shape[1], frame.shape[0]))
        _, faces = detector.detect(frame)

        if faces is not None:
            for face in faces:
                aligned_face = recognizer.alignCrop(frame, face)
                live_feature = recognizer.feature(aligned_face)

                best_student_id = None
                best_score = -1
                best_name = "UNKNOWN PERSON"

                for student_id, data in student_templates.items():
                    score = recognizer.match(
                        live_feature,
                        data["template"],
                        cv2.FaceRecognizerSF_FR_COSINE
                    )
                    if score > best_score:
                        best_score = score
                        best_student_id = student_id
                        best_name = data["name"]

                x, y, w, h = face[:4].astype(int)

                if best_score >= THRESHOLD:
                    label = best_name
                    color = (0, 255, 0)

                    if best_student_id not in marked_students:
                        cursor.execute(
                            """
                            INSERT IGNORE INTO attendance
                            (student_id, session_id, status)
                            VALUES (%s, %s, %s)
                            """,
                            (best_student_id, SESSION_ID, "Present")
                        )
                        connection.commit()

                        if cursor.rowcount == 1:
                            attendance_message = f"ATTENDANCE MARKED: {best_name}"
                            print(
                                f"Attendance recorded: {best_student_id} - {best_name}"
                            )
                        else:
                            attendance_message = f"ALREADY MARKED: {best_name}"
                            print(f"Attendance already exists: {best_student_id}")

                        marked_students.add(best_student_id)
                    else:
                        attendance_message = f"ALREADY MARKED: {best_name}"
                else:
                    label = "UNKNOWN PERSON"
                    color = (0, 0, 255)

                cv2.rectangle(frame, (x, y), (x + w, y + h), color, 2)
                cv2.putText(
                    frame,
                    f"{label} ({best_score:.2f})",
                    (x, max(y - 10, 25)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.7,
                    color,
                    2
                )

        cv2.putText(
            frame,
            attendance_message,
            (20, 40),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (0, 255, 0),
            2
        )

        cv2.imshow(window_name, frame)

        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

except Exception as error:
    print("ERROR:", error)

finally:
    if camera is not None:
        camera.release()

    cv2.destroyAllWindows()

    if cursor is not None:
        cursor.close()

    if connection is not None and connection.is_connected():
        connection.close()

    print("Program closed.")
