@preconcurrency import AVFoundation
import ImageIO
import Observation
import SwiftUI
import UniformTypeIdentifiers

/// "Skip 3D, just take photos": a plain camera at the sensor's largest photo size for objects Object Capture will
/// not lock onto (small parts, glossy or dark objects). Stills have no pose (`pose_reliable: false`, no camera),
/// the same shape tools/import_photos.py writes; the scan package has `depth_source: none` and no keyframes.
@MainActor
@Observable
final class PhotoCapture: NSObject {
    private(set) var count = 0
    private(set) var busy = false
    private(set) var error: String?
    private(set) var resolution = ""
    private(set) var running = false

    let record: SessionRecord
    let storage: ScanStorage
    let upload: UploadQueue?
    let diag: Diag
    let session = AVCaptureSession()
    let output = AVCapturePhotoOutput()
    private let queue = DispatchQueue(label: "photo-capture")
    private var stillIDs: [String] = []

    init(record: SessionRecord, storage: ScanStorage, upload: UploadQueue?, diag: Diag) {
        self.record = record
        self.storage = storage
        self.upload = upload
        self.diag = diag
    }

    func start() {
        guard let device = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .back),
              let input = try? AVCaptureDeviceInput(device: device) else {
            error = "No back camera."
            return
        }
        let dims = device.activeFormat.supportedMaxPhotoDimensions
        session.beginConfiguration()
        session.sessionPreset = .photo
        if session.canAddInput(input) { session.addInput(input) }
        if session.canAddOutput(output) { session.addOutput(output) }
        if let largest = dims.max(by: { Int($0.width) * Int($0.height) < Int($1.width) * Int($1.height) }) {
            output.maxPhotoDimensions = largest
            resolution = "\(largest.width)×\(largest.height)"
        }
        output.maxPhotoQualityPrioritization = .balanced
        session.commitConfiguration()
        let running = Unchecked(value: session)
        queue.async { running.value.startRunning() }
        self.running = true
        diag.log("photos_only_start", ["max_dims": resolution])
        // The package has no geometry and no keyframes: write it now so stills can upload as they are taken.
        writePackage()
    }

    func stop() {
        let s = Unchecked(value: session)
        queue.async { s.value.stopRunning() }
        running = false
    }

    func shoot() {
        guard !busy else { return }
        busy = true
        let settings = AVCapturePhotoSettings(format: [AVVideoCodecKey: AVVideoCodecType.jpeg])
        settings.maxPhotoDimensions = output.maxPhotoDimensions
        settings.photoQualityPrioritization = .balanced
        output.capturePhoto(with: settings, delegate: self)
    }

    /// Ends the session: marks the record captured and queues everything for upload.
    func finish() -> SessionRecord {
        stop()
        var r = record
        r.stillIDs = stillIDs
        r.status = .captured
        try? storage.save(r)
        upload?.enqueueAll(stillIDs: stillIDs, scanReady: true)
        diag.log("photos_only_done", ["photos": stillIDs.count])
        return r
    }

    private func writePackage() {
        let pkg = ScanPackage(
            alignment: nil, capturedAt: Date(), coverageHint: nil, cropBox: nil, depthSource: DepthSource.none,
            device: DeviceCapabilities.current().contract, frame: .arkitWorld, gravityArkitWorld: [0, -1, 0],
            keyframes: [], meshFile: nil, pointCount: nil, pointsFile: nil, poseSource: nil, productID: record.productID,
            scanID: record.scanID, segments: nil, sessionID: record.id, supportPlane: nil, supportingPlaneRemoved: false, symptom: record.symptom,
            units: .m, voxelMm: nil)
        do {
            try JSON.encoder.encode(pkg).write(to: storage.scanPackageURL(record), options: .atomic)
            upload?.enqueueAll(stillIDs: [], scanReady: true)
        } catch {
            self.error = error.localizedDescription
        }
    }

    fileprivate func saved(_ data: Data?, error: Error?) {
        busy = false
        guard let data else {
            self.error = error?.localizedDescription ?? "The photo failed."
            diag.log("error", ["message": self.error ?? ""])
            return
        }
        let id = "\(record.scanID)_s\(stillIDs.count + 1)"
        let dir = storage.stillsDir(record)
        let record = record
        Task.detached(priority: .userInitiated) {
            do {
                let still = try Self.writeStill(data, id: id, dir: dir, record: record)
                await MainActor.run {
                    self.stillIDs.append(id)
                    self.count = self.stillIDs.count
                    self.upload?.enqueueStill(id)
                    self.diag.log("photo", ["id": id, "w": still.widthPx, "h": still.heightPx])
                }
            } catch {
                await MainActor.run {
                    self.error = error.localizedDescription
                    self.diag.log("error", ["message": error.localizedDescription])
                }
            }
        }
    }

    /// Upright full-resolution JPEG with no metadata (no EXIF, no GPS), plus the still JSON. Intrinsics are
    /// estimated from the EXIF 35 mm focal length before it is dropped.
    nonisolated static func writeStill(_ data: Data, id: String, dir: URL, record: SessionRecord) throws -> Still {
        guard let src = CGImageSourceCreateWithData(data as CFData, nil),
              let props = CGImageSourceCopyPropertiesAtIndex(src, 0, nil) as? [CFString: Any],
              let w0 = props[kCGImagePropertyPixelWidth] as? Int, let h0 = props[kCGImagePropertyPixelHeight] as? Int,
              let image = CGImageSourceCreateThumbnailAtIndex(src, 0, [
                  kCGImageSourceCreateThumbnailFromImageAlways: true,
                  kCGImageSourceThumbnailMaxPixelSize: max(w0, h0),
                  kCGImageSourceCreateThumbnailWithTransform: true,
              ] as CFDictionary),
              let dest = CGImageDestinationCreateWithURL(dir.appending(path: "\(id).jpg") as CFURL,
                                                         UTType.jpeg.identifier as CFString, 1, nil)
        else { throw GatewayError.message("Could not read the photo.") }
        CGImageDestinationAddImage(dest, image, [kCGImageDestinationLossyCompressionQuality: 0.9] as CFDictionary)
        guard CGImageDestinationFinalize(dest) else { throw GatewayError.message("Could not save the photo.") }
        let w = image.width, h = image.height
        var k: StillIntrinsics?
        if let f35 = (props[kCGImagePropertyExifDictionary] as? [CFString: Any])?[kCGImagePropertyExifFocalLenIn35mmFilm] as? Double, f35 > 0 {
            let f = f35 / 36 * Double(max(w, h))
            k = StillIntrinsics(cx: Double(w) / 2, cy: Double(h) / 2, fx: f, fy: f)
        }
        let still = Still(
            attachedToTargetID: nil, autoFired: false, camera: nil, cameraDesign: nil, capturedAt: Date(),
            confidenceFile: nil, depthFile: nil, depthGapMS: nil, exposureS: nil, heightPx: h, imageFile: "stills/\(id).jpg", intrinsics: k, iso: nil,
            kind: .unguided, lowLight: nil, poseQuality: nil, poseReliable: false, poseSource: nil, scanID: record.scanID, segment: nil, sessionID: record.id, sharpness: nil,
            stillID: id, targets: [], torchLevel: nil, torchOn: nil, trackingState: .unknown, userSkipped: nil, viewCell: nil, widthPx: w)
        try JSON.encoder.encode(still).write(to: dir.appending(path: "\(id).json"), options: .atomic)
        return still
    }
}

extension PhotoCapture: AVCapturePhotoCaptureDelegate {
    nonisolated func photoOutput(_ output: AVCapturePhotoOutput, didFinishProcessingPhoto photo: AVCapturePhoto, error: Error?) {
        let data = photo.fileDataRepresentation()
        Task { @MainActor in self.saved(data, error: error) }
    }
}

struct CameraPreview: UIViewRepresentable {
    let session: AVCaptureSession

    final class PreviewView: UIView {
        override class var layerClass: AnyClass { AVCaptureVideoPreviewLayer.self }
        var preview: AVCaptureVideoPreviewLayer { layer as! AVCaptureVideoPreviewLayer }
    }

    func makeUIView(context: Context) -> PreviewView {
        let v = PreviewView()
        v.preview.session = session
        v.preview.videoGravity = .resizeAspectFill
        return v
    }

    func updateUIView(_ v: PreviewView, context: Context) {}
}

struct PhotoCaptureView: View {
    @Bindable var capture: PhotoCapture
    let close: () -> Void
    let finished: (SessionRecord) -> Void

    var body: some View {
        ZStack {
            CameraPreview(session: capture.session).ignoresSafeArea()
            VStack(spacing: 12) {
                HStack {
                    Button(action: { capture.stop(); close() }) {
                        Image(systemName: "xmark").font(.headline).padding(10).background(.ultraThinMaterial, in: Circle())
                    }
                    Spacer()
                    Text("\(capture.count)").font(.headline.monospacedDigit())
                        .padding(10).background(.ultraThinMaterial, in: Capsule())
                }
                Spacer()
                Text(capture.error ?? "Photograph anything that looks damaged or important")
                    .font(.title2.bold()).multilineTextAlignment(.center)
                    .padding(.horizontal, 16).padding(.vertical, 10)
                    .background(.ultraThinMaterial, in: Capsule())
                HStack(spacing: 20) {
                    Button(action: capture.shoot) {
                        Circle().strokeBorder(.white, lineWidth: 4).frame(width: 72, height: 72)
                            .overlay(Circle().fill(.white.opacity(capture.busy ? 0.3 : 0.9)).padding(8))
                    }
                    .disabled(capture.busy)
                    Button("Finish") { finished(capture.finish()) }
                        .buttonStyle(.borderedProminent).disabled(capture.count == 0)
                }
            }
            .padding()
        }
        .sensoryFeedback(.success, trigger: capture.count)
        .onAppear { capture.start() }
        .statusBarHidden()
    }
}
