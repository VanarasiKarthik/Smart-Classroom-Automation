
import cv2
import mediapipe as mp
import time

# Load MediaPipe Tasks
BaseOptions = mp.tasks.BaseOptions
FaceDetector = mp.tasks.vision.FaceDetector
FaceDetectorOptions = mp.tasks.vision.FaceDetectorOptions
RunningMode = mp.tasks.vision.RunningMode

# Configure face detector
options = FaceDetectorOptions(
    base_options=BaseOptions(
        model_asset_path="blaze_face_short_range.tflite"
    ),
    running_mode=RunningMode.VIDEO,
    min_detection_confidence=0.6
)

# Start laptop camera
camera = cv2.VideoCapture(0)

if not camera.isOpened():
    print("Laptop camera not detected")
    exit()

print("AI Face Detection Started")
print("Press Q to close")

timestamp_ms = 0

try:
    with FaceDetector.create_from_options(options) as detector:

        while True:
            success, frame = camera.read()

            if not success:
                print("Camera reading failed")
                break

            # Convert BGR to RGB
            rgb_frame = cv2.cvtColor(
                frame, cv2.COLOR_BGR2RGB
            )

            # Create MediaPipe image
            mp_image = mp.Image(
                image_format=mp.ImageFormat.SRGB,
                data=rgb_frame
            )

            # Process video frame
            timestamp_ms += 33
            results = detector.detect_for_video(
                mp_image, timestamp_ms
            )

            # Draw face detection boxes
            if results.detections:

                for detection in results.detections:

                    box = detection.bounding_box

                    x = box.origin_x
                    y = box.origin_y
                    w = box.width
                    h = box.height

                    confidence = detection.categories[0].score

                    cv2.rectangle(
                        frame,
                        (x, y),
                        (x + w, y + h),
                        (0, 255, 0),
                        2
                    )

                    cv2.putText(
                        frame,
                        f"Face: {confidence:.2f}",
                        (x, y - 10),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.6,
                        (0, 255, 0),
                        2
                    )

                cv2.putText(
                    frame,
                    "FACE DETECTED",
                    (20, 35),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.8,
                    (0, 255, 0),
                    2
                )

            else:
                cv2.putText(
                    frame,
                    "NO FACE DETECTED",
                    (20, 35),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.8,
                    (0, 0, 255),
                    2
                )

            cv2.imshow(
                "AI Smart Classroom - Face Detection",
                frame
            )

            if cv2.waitKey(1) & 0xFF == ord('q'):
                break

finally:
    camera.release()
    cv2.destroyAllWindows()
