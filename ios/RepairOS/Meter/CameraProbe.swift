@preconcurrency import AVFoundation
import Foundation

/// The most recent camera frames, written from the capture queue and read from the main actor.
final class FrameBuffer: @unchecked Sendable {
    private let lock = NSLock()
    private var frames: [CameraFrame] = []
    private let keepSeconds: Double

    init(keepSeconds: Double) { self.keepSeconds = keepSeconds }

    func append(_ frame: CameraFrame) {
        lock.lock()
        defer { lock.unlock() }
        frames.append(frame)
        var drop = 0
        while drop < frames.count - 1, frame.t - frames[drop].t > keepSeconds { drop += 1 }
        if drop > 0 { frames.removeFirst(drop) }
    }

    func snapshot() -> [CameraFrame] {
        lock.lock()
        defer { lock.unlock() }
        return frames
    }

    func clear() {
        lock.lock()
        defer { lock.unlock() }
        frames.removeAll()
    }
}

enum CameraProbeError: Error, Equatable {
    case denied, noCamera, cannotStart
    /// `stop()` was called before the camera got going: nothing was started, and nothing is wrong.
    case stopped
}

/// The back camera as a sensor: it reads frames at the highest frame rate up to `targetFPS` and keeps each one as a
/// grid of cells (`CellGrid`), never the picture. Not run in the simulator, which has no camera: the logic it feeds is
/// unit-tested on synthetic frames. The permission prompt appears when the person starts a camera check.
final class CameraProbe: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate, @unchecked Sendable {
    let buffer: FrameBuffer
    let session = AVCaptureSession()
    private let queue = DispatchQueue(label: "lea.phonecheck.camera", qos: .userInitiated)
    private var device: AVCaptureDevice?   // only touched on `queue`
    private var configured = false          // only touched on `queue`
    private let wantedLock = NSLock()
    private var wanted = false               // the last of `start` and `stop` that was called

    /// `storeColour: false` keeps only each cell's brightness (a third of the memory), for checks that never name a colour.
    init(keepSeconds: Double, storeColour: Bool = true) {
        buffer = FrameBuffer(keepSeconds: keepSeconds)
        self.storeColour = storeColour
        super.init()
    }

    private let storeColour: Bool

    static func authorized() async -> Bool {
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized: return true
        case .notDetermined: return await AVCaptureDevice.requestAccess(for: .video)
        default: return false
        }
    }

    /// Starts the camera and returns the frame rate it runs at (the highest the phone offers, up to `targetFPS`).
    func start(targetFPS: Double) async throws -> Double {
        // A check that was stopped before this task got going must not turn the camera on.
        guard !Task.isCancelled else { throw CameraProbeError.stopped }
        setWanted(true)
        guard await Self.authorized() else { throw CameraProbeError.denied }
        guard !Task.isCancelled else {
            setWanted(false)
            throw CameraProbeError.stopped
        }
        buffer.clear()
        return try await withCheckedThrowingContinuation { (c: CheckedContinuation<Double, Error>) in
            queue.async {
                do { c.resume(returning: try self.run(targetFPS: targetFPS)) } catch { c.resume(throwing: error) }
            }
        }
    }

    func stop() {
        setWanted(false)
        queue.async {
            if self.session.isRunning { self.session.stopRunning() }
        }
    }

    private func setWanted(_ on: Bool) {
        wantedLock.lock()
        wanted = on
        wantedLock.unlock()
    }

    private var isWanted: Bool {
        wantedLock.lock()
        defer { wantedLock.unlock() }
        return wanted
    }

    private func run(targetFPS: Double) throws -> Double {
        guard isWanted else { throw CameraProbeError.stopped }
        if !configured {
            guard let dev = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .back) else {
                throw CameraProbeError.noCamera
            }
            let input = try AVCaptureDeviceInput(device: dev)
            session.beginConfiguration()
            session.sessionPreset = .inputPriority
            let output = AVCaptureVideoDataOutput()
            output.videoSettings = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA]
            output.alwaysDiscardsLateVideoFrames = true
            output.setSampleBufferDelegate(self, queue: queue)
            guard session.canAddInput(input), session.canAddOutput(output) else {
                session.commitConfiguration()
                throw CameraProbeError.cannotStart
            }
            session.addInput(input)
            session.addOutput(output)
            session.commitConfiguration()
            device = dev
            configured = true
        }
        guard let dev = device else { throw CameraProbeError.noCamera }
        var fps = 30.0
        do {
            try dev.lockForConfiguration()
            let best = Self.bestFormat(dev, targetFPS: targetFPS)
            dev.activeFormat = best.format
            fps = min(targetFPS, best.maxFPS)
            let frame = CMTime(value: 1, timescale: CMTimeScale(fps.rounded()))
            dev.activeVideoMinFrameDuration = frame
            dev.activeVideoMaxFrameDuration = frame
            dev.unlockForConfiguration()
        } catch {
            throw CameraProbeError.cannotStart
        }
        if !session.isRunning { session.startRunning() }
        return fps
    }

    /// The smallest picture (at least 640 wide) that reaches `targetFPS`; if none does, the one with the highest rate.
    static func bestFormat(_ dev: AVCaptureDevice, targetFPS: Double) -> (format: AVCaptureDevice.Format, maxFPS: Double) {
        func width(_ f: AVCaptureDevice.Format) -> Int { Int(CMVideoFormatDescriptionGetDimensions(f.formatDescription).width) }
        func rate(_ f: AVCaptureDevice.Format) -> Double { f.videoSupportedFrameRateRanges.map(\.maxFrameRate).max() ?? 0 }
        let usable = dev.formats.filter { width($0) >= 640 }
        let pool = usable.isEmpty ? dev.formats : usable
        let reaching = pool.filter { rate($0) >= targetFPS }
        let chosen = reaching.min { width($0) < width($1) } ?? pool.max { rate($0) < rate($1) } ?? dev.activeFormat
        return (chosen, rate(chosen))
    }

    // MARK: Frames

    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        guard let pixels = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        CVPixelBufferLockBaseAddress(pixels, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(pixels, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddress(pixels) else { return }
        let reduced = CellGrid.reduce(base: base.assumingMemoryBound(to: UInt8.self), width: CVPixelBufferGetWidth(pixels),
                                      height: CVPixelBufferGetHeight(pixels), bytesPerRow: CVPixelBufferGetBytesPerRow(pixels),
                                      colour: storeColour)
        let t = CMTimeGetSeconds(CMSampleBufferGetPresentationTimeStamp(sampleBuffer))
        buffer.append(CameraFrame(t: t, luma: reduced.luma, rgb: reduced.rgb, mean: reduced.mean,
                                  iso: device?.iso ?? 0, exposureS: device.map { CMTimeGetSeconds($0.exposureDuration) } ?? 0))
    }
}

enum PhoneCheckWords {
    static func camera(_ error: Error) -> String {
        switch error as? CameraProbeError {
        case .denied: "The camera is off for this app. Turn it on in Settings > Privacy & Security > Camera."
        case .noCamera: "This phone has no camera this check can use."
        case .stopped: "The camera was stopped."
        default: "The camera did not start. Close other apps that use it and try again."
        }
    }

    static let microphoneDenied = "The microphone is off for this app. Turn it on in Settings > Privacy & Security > Microphone."
    static let microphoneFailed = "The microphone did not start. Close other apps that use it and try again."
    static let noMagnetometer = "This phone has no magnetometer this check can use."
}
