import AVFoundation
import Combine
import CoreImage
import SwiftUI

@MainActor
final class CameraService: NSObject, ObservableObject, AVCapturePhotoCaptureDelegate {
    @Published private(set) var isReady = false
    @Published var message: String?
    var onPhoto: ((Data) -> Void)?

    private let controller = CameraSessionController()
    var session: AVCaptureSession { controller.session }

    func start() {
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized: configureAndStart()
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .video) { [weak self] granted in
                DispatchQueue.main.async {
                    if granted { self?.configureAndStart() }
                    else { self?.message = "Camera access is needed to capture the object. Enable it in Settings." }
                }
            }
        default:
            message = "Camera access is needed to capture the object. Enable it in Settings."
        }
    }

    func stop() {
        isReady = false
        controller.stop()
    }

    func takePhoto() {
        guard isReady else { return }
        controller.takePhoto(delegate: self)
    }

    /// Zooms toward a part (box: fractions of the upright frame) as far as keeps it in view, and focuses on it.
    /// Digital zoom crops toward the centre, so an off-centre part limits how far it can go.
    func zoom(to box: AgentAction.Box?) { controller.zoom(to: box) }

    /// The live view as a portrait JPEG (long side `maxSide`), for coaching only. Nil if the camera is not running.
    func liveFrame(maxSide: CGFloat = 1024) async -> (jpeg: Data, size: CGSize)? {
        guard isReady else { return nil }
        return await controller.grabFrame(maxSide: maxSide)
    }

    private func configureAndStart() {
        controller.start { [weak self] ready in
            self?.isReady = ready
            if !ready { self?.message = "No back camera is available on this device." }
        }
    }

    nonisolated func photoOutput(_ output: AVCapturePhotoOutput, didFinishProcessingPhoto photo: AVCapturePhoto, error: Error?) {
        let data = photo.fileDataRepresentation()
        DispatchQueue.main.async {
            if let error { self.message = "Capture failed: \(error.localizedDescription)" }
            else if let data { self.onPhoto?(data) }
            else { self.message = "The camera returned no photo data." }
        }
    }
}

private final class CameraSessionController: @unchecked Sendable {
    let session = AVCaptureSession()
    private let output = AVCapturePhotoOutput()
    private let frames = AVCaptureVideoDataOutput()
    private let grabber = FrameGrabber()
    private let queue = DispatchQueue(label: "app.lea.camera")
    private let frameQueue = DispatchQueue(label: "app.lea.camera.frames")
    private var configured = false

    func start(completion: @escaping @MainActor (Bool) -> Void) {
        queue.async {
            if !self.configured {
                self.session.beginConfiguration()
                self.session.sessionPreset = .photo
                guard let camera = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .back),
                      let input = try? AVCaptureDeviceInput(device: camera),
                      self.session.canAddInput(input), self.session.canAddOutput(self.output) else {
                    self.session.commitConfiguration()
                    DispatchQueue.main.async { completion(false) }
                    return
                }
                self.session.addInput(input)
                self.session.addOutput(self.output)
                self.frames.alwaysDiscardsLateVideoFrames = true
                self.frames.videoSettings = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA]
                if self.session.canAddOutput(self.frames) {
                    self.session.addOutput(self.frames)
                    self.frames.setSampleBufferDelegate(self.grabber, queue: self.frameQueue)
                }
                self.configured = true
                self.session.commitConfiguration()
            }
            if !self.session.isRunning { self.session.startRunning() }
            let running = self.session.isRunning
            DispatchQueue.main.async { completion(running) }
        }
    }

    func stop() {
        queue.async { if self.session.isRunning { self.session.stopRunning() } }
    }

    func zoom(to box: AgentAction.Box?) {
        queue.async {
            guard let device = (self.session.inputs.first as? AVCaptureDeviceInput)?.device, (try? device.lockForConfiguration()) != nil else { return }
            defer { device.unlockForConfiguration() }
            guard let box else { device.videoZoomFactor = 1; return }
            let cx = (box.left + box.right) / 2, cy = (box.top + box.bottom) / 2
            let half = max(box.right - box.left, box.bottom - box.top) / 2
            let fits = 0.5 / max(abs(cx - 0.5) + half, 0.05)            // largest zoom that keeps the box on screen
            let wanted = 0.6 / max(2 * half, 0.05)                      // the part fills ~60% of the view
            device.videoZoomFactor = max(1, min(fits * 0.9, wanted, device.activeFormat.videoMaxZoomFactor, 6))
            // Upright (portrait) fractions back to the sensor's landscape coordinates.
            let point = CGPoint(x: cy, y: 1 - cx)
            if device.isFocusPointOfInterestSupported { device.focusPointOfInterest = point; device.focusMode = .autoFocus }
            if device.isExposurePointOfInterestSupported { device.exposurePointOfInterest = point; device.exposureMode = .autoExpose }
        }
    }

    func grabFrame(maxSide: CGFloat) async -> (jpeg: Data, size: CGSize)? {
        await withCheckedContinuation { c in grabber.next(maxSide: maxSide) { c.resume(returning: $0) } }
    }

    func takePhoto(delegate: AVCapturePhotoCaptureDelegate) {
        queue.async {
            let settings = AVCapturePhotoSettings(format: [AVVideoCodecKey: AVVideoCodecType.jpeg])
            self.output.capturePhoto(with: settings, delegate: delegate)
        }
    }
}

/// Hands out the next video frame on request, as a portrait JPEG. Frames are never stored.
nonisolated private final class FrameGrabber: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate, @unchecked Sendable {
    private let lock = NSLock()
    private var waiting: [(CGFloat, ((jpeg: Data, size: CGSize)?) -> Void)] = []
    private let context = CIContext()

    func next(maxSide: CGFloat, _ done: @escaping ((jpeg: Data, size: CGSize)?) -> Void) {
        lock.withLock { waiting.append((maxSide, done)) }
    }

    nonisolated func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        let pending = lock.withLock { () -> [(CGFloat, ((jpeg: Data, size: CGSize)?) -> Void)] in let w = waiting; waiting = []; return w }
        guard !pending.isEmpty else { return }
        guard let pixels = CMSampleBufferGetImageBuffer(sampleBuffer) else { pending.forEach { $0.1(nil) }; return }
        // The back camera delivers landscape frames; .right turns them upright for a phone held in portrait.
        let upright = CIImage(cvPixelBuffer: pixels).oriented(.right)
        for (maxSide, done) in pending {
            let scale = min(1, maxSide / max(upright.extent.width, upright.extent.height))
            let image = upright.transformed(by: CGAffineTransform(scaleX: scale, y: scale))
            let jpeg = context.jpegRepresentation(of: image, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!,
                                                  options: [kCGImageDestinationLossyCompressionQuality as CIImageRepresentationOption: 0.8])
            done(jpeg.map { ($0, image.extent.size) })
        }
    }
}

struct CameraPreview: UIViewRepresentable {
    let session: AVCaptureSession

    func makeUIView(context: Context) -> PreviewView {
        let view = PreviewView()
        view.previewLayer.session = session
        view.previewLayer.videoGravity = .resizeAspectFill
        return view
    }

    func updateUIView(_ uiView: PreviewView, context: Context) {}
}

final class PreviewView: UIView {
    override class var layerClass: AnyClass { AVCaptureVideoPreviewLayer.self }
    var previewLayer: AVCaptureVideoPreviewLayer { layer as! AVCaptureVideoPreviewLayer }
}
