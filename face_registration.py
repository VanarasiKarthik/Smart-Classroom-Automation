
import cv2
import mediapipe as mp
import os
import sys

if len(sys.argv) < 2:
    print("Student ID not provided")
    sys.exit(1)

STUDENT_ID = sys.argv[1]
SAVE_PATH = os.path.join("dataset", STUDENT_ID)

os.makedirs(SAVE_PATH, exist_ok=True)

BaseOptions = mp.tasks.BaseOptions
FaceDetector = mp.tasks.vision.FaceDetector
FaceDetectorOptions = mp.tasks.vision.FaceDetectorOptions
RunningMode = mp.tasks.vision.RunningMode

options = FaceDetectorOptions(
    base_options=BaseOptions(
        model_asset_path="blaze_face_short_range.tflite"
    ),
    running_mode=RunningMode.VIDEO,
    min_detection_confidence=0.6
)

camera = cv2.VideoCapture(0)

if not camera.isOpened():
    print("Camera not detected")
    sys.exit(1)

count = 0
timestamp_ms = 0

print(f"Face Registration Started: {STUDENT_ID}")
print("Press S to save a face sample")
print("Press Q to quit")

try:
    with FaceDetector.create_from_options(options) as detector:

        while count < 10:
            success, frame = camera.read()

            if not success:
                print("Camera reading failed")
                break

            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

            mp_image = mp.Image(
                image_format=mp.ImageFormat.SRGB,
                data=rgb
            )

            timestamp_ms += 33
            results = detector.detect_for_video(mp_image, timestamp_ms)

            face_crop = None

            if results.detections:
                detection = max(
                    results.detections,
                    key=lambda d: d.bounding_box.width *
                    d.bounding_box.height
                )

                box = detection.bounding_box

                x1 = max(0, box.origin_x)
                y1 = max(0, box.origin_y)
                x2 = min(frame.shape[1], x1 + box.width)
                y2 = min(frame.shape[0], y1 + box.height)

                if x2 > x1 and y2 > y1:
                    face_crop = frame[y1:y2, x1:x2].copy()

                    cv2.rectangle(
                        frame, (x1, y1), (x2, y2), (0, 255, 0), 2
                    )

            cv2.putText(
                frame, f"Samples: {count}/10",
                (20, 35), cv2.FONT_HERSHEY_SIMPLEX,
                0.8, (0, 255, 0), 2
            )

            cv2.putText(
                frame, "S: SAVE SAMPLE | Q: QUIT",
                (20, 70), cv2.FONT_HERSHEY_SIMPLEX,
                0.6, (255, 255, 255), 2
            )

            cv2.imshow("Student Face Registration", frame)

            key = cv2.waitKey(1) & 0xFF

            if key == ord('s'):
                if face_crop is not None:
                    filename = os.path.join(
                        SAVE_PATH, f"face_{count + 1}.jpg"
                    )

                    if cv2.imwrite(filename, face_crop):
                        count += 1
                        print(f"Saved sample {count}/10")
                else:
                    print("No face detected")

            elif key == ord('q'):
                break

finally:
    camera.release()
    cv2.destroyAllWindows()

print(f"Registration finished. Samples saved: {count}/10")
