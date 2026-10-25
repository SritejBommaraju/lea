import AVFoundation
import Foundation
import ImageIO
import RealityKit
import UniformTypeIdentifiers
import simd

/// Turns an Object Capture image folder into our contracts: PhotogrammetrySession (on device) solves camera poses
/// and a point cloud, plus a reduced USDZ for a viewer. Orbit images become scan keyframes (JPEG, ≤1920 px),
/// close-ups become full-resolution `unguided` stills. Intrinsics come from each HEIC's depth calibration data,
/// else from the EXIF 35 mm focal length. Images keep their stored pixel orientation so intrinsics match.
struct Reconstruction: Sendable {
    let imagesDir: URL
    let checkpointDir: URL
    let scanDir: URL
    let stillsDir: URL
    let record: SessionRecord
    let closeupFiles: Set<String>
    let diag: Diag
    let startedAt: Date
    let device: Device
    let progress: @Sendable (String, Double) -> Void

    struct Output: Sendable {
        var keyframes: Int
        var stillIDs: [String]
        var points: Int
    }

    enum Failure: LocalizedError {
        case unsupported, noPoses, message(String)
        var errorDescription: String? {
            switch self {
            case .unsupported: "Photogrammetry is not supported on this phone."
            case .noPoses: "Photogrammetry returned no camera poses."
            case .message(let m): m
            }
        }
    }

    func run() async throws -> Output {
        guard PhotogrammetrySession.isSupported else { throw Failure.unsupported }
        var config = PhotogrammetrySession.Configuration(checkpointDirectory: checkpointDir)
        config.isObjectMaskingEnabled = true
        let session = try PhotogrammetrySession(input: imagesDir, configuration: config)
        let modelURL = scanDir.appending(path: "model.usdz")
        var poses: PhotogrammetrySession.Poses?
        var cloud: PhotogrammetrySession.PointCloud?
        try session.process(requests: [.poses, .pointCloud, .modelFile(url: modelURL, detail: .reduced)])
        diag.log("pg_start", ["limits_images": PhotogrammetrySession.limits.maximumNumberOfInputImages,
                              "limits_dim": PhotogrammetrySession.limits.maximumInputImageDimension])
        var lastStage = ""
        for try await output in session.outputs {
            switch output {
            case .requestProgress(let req, let f):
                progress(Self.words(req), f)
            case .requestProgressInfo(let req, let info):
                let stage = info.processingStage.map { "\($0)" } ?? ""
                if stage != lastStage {
                    lastStage = stage
                    diag.log("pg_stage", ["request": "\(req)", "stage": stage, "eta_s": info.estimatedRemainingTime ?? -1])
                }
            case .requestComplete(let req, let result):
                switch result {
                case .poses(let p):
                    poses = p
                    diag.log("pg_poses", ["count": p.posesBySample.count])
                case .pointCloud(let c):
                    cloud = c
                    diag.log("pg_points", ["count": c.points.count])
                case .modelFile(let url):
                    diag.log("pg_model", ["file": url.lastPathComponent])
                default:
                    diag.log("pg_result", ["request": "\(req)"])
                }
            case .requestError(let req, let error):
                diag.log("pg_request_error", ["request": "\(req)", "error": error.localizedDescription])
            case .invalidSample(let id, let reason):
                diag.log("pg_invalid_sample", ["id": id, "reason": reason])
            case .skippedSample(let id):
                diag.log("pg_skipped_sample", ["id": id])
            case .automaticDownsampling:
                diag.log("pg_downsampling")
            case .stitchingIncomplete:
                diag.log("pg_stitching_incomplete")
            case .processingComplete, .processingCancelled:
                diag.log("pg_done", ["output": "\(output)"])
            default:
                break
            }
            if case .processingComplete = output { break }
            if case .processingCancelled = output { throw Failure.message("Photogrammetry was cancelled.") }
        }
        guard let poses, !poses.posesBySample.isEmpty else { throw Failure.noPoses }
        progress("Writing the scan…", 1)
        return try write(poses: poses, cloud: cloud)
    }

    static func words(_ r: PhotogrammetrySession.Request) -> String {
        switch r {
        case .poses: "Working out camera positions…"
        case .pointCloud: "Building the point cloud…"
        case .modelFile: "Building the 3D model…"
        default: "Processing…"
        }
    }

    // MARK: - Contracts

    private struct Sample {
        let url: URL
        let cameraToWorld: simd_float4x4
        let width: Int
        let height: Int
        let k: (fx: Double, fy: Double, cx: Double, cy: Double)
        let kSource: String
        let date: Date
    }

    private func write(poses: PhotogrammetrySession.Poses, cloud: PhotogrammetrySession.PointCloud?) throws -> Output {
        let urls = poses.urlsBySample
        var samples: [Sample] = []
        for (id, pose) in poses.posesBySample.sorted(by: { $0.key < $1.key }) {
            guard let url = urls[id], let meta = Self.imageMeta(url) else {
                diag.log("sample_skipped", ["id": id, "reason": "no image or metadata"])
                continue
            }
            samples.append(Sample(url: url, cameraToWorld: pose.transform.matrix, width: meta.width, height: meta.height,
                                  k: meta.k, kSource: meta.kSource, date: meta.date))
        }
        guard !samples.isEmpty else { throw Failure.noPoses }

        // Which camera axis convention the poses use is not documented: test both against the point cloud and
        // keep the one that puts the cloud inside the images. Output is ARKit's (camera looks down -Z).
        let flip = convention(samples: samples, cloud: cloud)

        var points = 0
        var pointsFile: String?
        if let cloud, !cloud.points.isEmpty {
            let url = scanDir.appending(path: "points_arkit_world.ply")
            points = try PLYWriter.write(cloud.points.map(\.position), colors: cloud.points.map { SIMD3($0.color.x, $0.color.y, $0.color.z) }, to: url)
            pointsFile = "scans/\(record.scanID)/\(url.lastPathComponent)"
        }

        var keyframes: [ScanPackageSchema] = []
        var stillIDs: [String] = []
        for (i, s) in samples.enumerated() {
            try autoreleasepool {
                let m = flip ? s.cameraToWorld * Self.flipYZ : s.cameraToWorld
                let q = simd_quatf(m)
                let position = [Double(m.columns.3.x), Double(m.columns.3.y), Double(m.columns.3.z)]
                let quat = [Double(q.imag.x), Double(q.imag.y), Double(q.imag.z), Double(q.real)]
                if closeupFiles.contains(s.url.lastPathComponent) {
                    let id = "\(record.scanID)_s\(stillIDs.count + 1)"
                    let (w, h) = try Self.writeJPEG(from: s.url, to: stillsDir.appending(path: "\(id).jpg"), maxPixel: nil, quality: 0.9)
                    let sx = Double(w) / Double(s.width), sy = Double(h) / Double(s.height)
                    let still = Still(
                        attachedToTargetID: nil, autoFired: false,
                        camera: StillCamera(frame: .arkitWorld, positionM: position, quaternionXyzw: quat),
                        cameraDesign: nil, capturedAt: s.date, confidenceFile: nil, depthFile: nil, depthGapMS: nil, exposureS: nil, heightPx: h,
                        imageFile: "stills/\(id).jpg",
                        intrinsics: StillIntrinsics(cx: s.k.cx * sx, cy: s.k.cy * sy, fx: s.k.fx * sx, fy: s.k.fy * sy), iso: nil,
                        kind: .unguided, lowLight: nil, poseQuality: nil, poseReliable: nil, poseSource: nil, scanID: record.scanID, segment: nil, sessionID: record.id, sharpness: nil,
                        stillID: id, targets: [], torchLevel: nil, torchOn: nil, trackingState: .unknown, userSkipped: nil, viewCell: nil, widthPx: w)
                    try JSON.encoder.encode(still).write(to: stillsDir.appending(path: "\(id).json"), options: .atomic)
                    stillIDs.append(id)
                } else {
                    let id = String(format: "kf_%03d", keyframes.count)
                    let (w, h) = try Self.writeJPEG(from: s.url, to: scanDir.appending(path: "\(id).jpg"), maxPixel: 1920, quality: 0.85)
                    let sx = Double(w) / Double(s.width), sy = Double(h) / Double(s.height)
                    keyframes.append(ScanPackageSchema(
                        camera: KeyframeCamera(frame: .arkitWorld, positionM: position, quaternionXyzw: quat),
                        confidenceFile: nil, depthEncoding: nil, depthFile: nil, exposureS: nil,
                        imageFile: "scans/\(record.scanID)/\(id).jpg",
                        intrinsics: KeyframeIntrinsics(cx: s.k.cx * sx, cy: s.k.cy * sy, fx: s.k.fx * sx, fy: s.k.fy * sy,
                                                       heightPx: h, widthPx: w),
                        keyframeID: id, poseQuality: nil, poseSource: nil, segment: nil, sharpness: nil, timestampS: max(0, s.date.timeIntervalSince(startedAt)),
                        trackingState: .unknown))
                }
                if i % 10 == 0 { progress("Writing the scan…", Double(i) / Double(samples.count)) }
            }
        }
        diag.log("intrinsics_source", ["values": Array(Set(samples.map(\.kSource)))])

        let pkg = ScanPackage(
            alignment: nil, capturedAt: startedAt, coverageHint: nil, cropBox: nil, depthSource: .lidar,
            device: device,
            frame: .arkitWorld, gravityArkitWorld: [0, -1, 0], keyframes: keyframes, meshFile: nil,
            pointCount: pointsFile == nil ? nil : points, pointsFile: pointsFile, poseSource: .photogrammetry,
            productID: record.productID, scanID: record.scanID, segments: nil, sessionID: record.id, supportPlane: nil, supportingPlaneRemoved: nil,
            symptom: record.symptom, units: .m, voxelMm: nil)
        // A scan with poses but no points is a no-depth package; say so rather than claim a cloud.
        let final = pointsFile == nil ? ScanPackage(
            alignment: nil, capturedAt: pkg.capturedAt, coverageHint: nil, cropBox: nil, depthSource: DepthSource.none,
            device: pkg.device, frame: .arkitWorld, gravityArkitWorld: [0, -1, 0], keyframes: keyframes, meshFile: nil,
            pointCount: nil, pointsFile: nil, poseSource: .photogrammetry, productID: pkg.productID, scanID: pkg.scanID, segments: nil,
            sessionID: pkg.sessionID, supportPlane: nil, supportingPlaneRemoved: nil, symptom: pkg.symptom, units: .m, voxelMm: nil) : pkg
        try JSON.encoder.encode(final).write(to: scanDir.appending(path: "scan_package.json"), options: .atomic)
        return Output(keyframes: keyframes.count, stillIDs: stillIDs, points: points)
    }

    /// Rotation of 180° about the camera's X axis: OpenCV-style (+Z forward, +Y down) to ARKit (-Z forward, +Y up).
    static let flipYZ = simd_float4x4(diagonal: SIMD4(1, -1, -1, 1))

    /// True when the poses look OpenCV-style. Projects a sample of the cloud into up to 12 images both ways.
    private func convention(samples: [Sample], cloud: PhotogrammetrySession.PointCloud?) -> Bool {
        guard let cloud, !cloud.points.isEmpty else {
            diag.log("pose_convention", ["result": "unknown (no cloud), kept as-is"])
            return false
        }
        let step = max(1, cloud.points.count / 2000)
        let pts = stride(from: 0, to: cloud.points.count, by: step).map { cloud.points[$0].position }
        func inside(_ s: Sample, flip: Bool) -> Double {
            let m = simd_inverse(flip ? s.cameraToWorld * Self.flipYZ : s.cameraToWorld)
            var hit = 0
            for p in pts {
                let c = m * SIMD4(p, 1)
                guard c.z < 0 else { continue }
                let u = s.k.fx * Double(c.x / -c.z) + s.k.cx
                let v = -s.k.fy * Double(c.y / -c.z) + s.k.cy
                if u >= 0, u < Double(s.width), v >= 0, v < Double(s.height) { hit += 1 }
            }
            return Double(hit) / Double(pts.count)
        }
        let probe = stride(from: 0, to: samples.count, by: max(1, samples.count / 12)).map { samples[$0] }
        let asIs = probe.map { inside($0, flip: false) }.reduce(0, +) / Double(probe.count)
        let flipped = probe.map { inside($0, flip: true) }.reduce(0, +) / Double(probe.count)
        diag.log("pose_convention", ["inside_as_is": asIs, "inside_flipped": flipped, "flip": flipped > asIs])
        return flipped > asIs
    }

    // MARK: - Images

    private struct Meta {
        let width: Int, height: Int
        let k: (fx: Double, fy: Double, cx: Double, cy: Double)
        let kSource: String
        let date: Date
    }

    private static func imageMeta(_ url: URL) -> Meta? {
        guard let src = CGImageSourceCreateWithURL(url as CFURL, nil),
              let props = CGImageSourceCopyPropertiesAtIndex(src, 0, nil) as? [CFString: Any],
              let w = props[kCGImagePropertyPixelWidth] as? Int, let h = props[kCGImagePropertyPixelHeight] as? Int
        else { return nil }
        let date = (try? url.resourceValues(forKeys: [.creationDateKey]).creationDate) ?? Date()
        // Exact intrinsics from the depth calibration Apple stores with the image, when present.
        for type in [kCGImageAuxiliaryDataTypeDepth, kCGImageAuxiliaryDataTypeDisparity] {
            if let aux = CGImageSourceCopyAuxiliaryDataInfoAtIndex(src, 0, type) as? [AnyHashable: Any],
               let depth = try? AVDepthData(fromDictionaryRepresentation: aux),
               let cal = depth.cameraCalibrationData {
                let m = cal.intrinsicMatrix
                let ref = cal.intrinsicMatrixReferenceDimensions
                let sx = Double(w) / Double(ref.width), sy = Double(h) / Double(ref.height)
                return Meta(width: w, height: h,
                            k: (Double(m[0][0]) * sx, Double(m[1][1]) * sy, Double(m[2][0]) * sx, Double(m[2][1]) * sy),
                            kSource: "depth_calibration", date: date)
            }
        }
        let exif = props[kCGImagePropertyExifDictionary] as? [CFString: Any]
        guard let f35 = exif?[kCGImagePropertyExifFocalLenIn35mmFilm] as? Double, f35 > 0 else { return nil }
        let f = f35 / 36 * Double(max(w, h))
        return Meta(width: w, height: h, k: (f, f, Double(w) / 2, Double(h) / 2), kSource: "exif_35mm", date: date)
    }

    /// Re-encodes an image as JPEG in its stored pixel orientation, optionally downscaled, with no metadata.
    static func writeJPEG(from src: URL, to dst: URL, maxPixel: Int?, quality: Double) throws -> (Int, Int) {
        guard let source = CGImageSourceCreateWithURL(src as CFURL, nil) else { throw Failure.message("Cannot read \(src.lastPathComponent)") }
        let image: CGImage?
        if let maxPixel {
            image = CGImageSourceCreateThumbnailAtIndex(source, 0, [
                kCGImageSourceCreateThumbnailFromImageAlways: true,
                kCGImageSourceThumbnailMaxPixelSize: maxPixel,
                kCGImageSourceCreateThumbnailWithTransform: false,
            ] as CFDictionary)
        } else {
            image = CGImageSourceCreateImageAtIndex(source, 0, nil)
        }
        guard let image, let dest = CGImageDestinationCreateWithURL(dst as CFURL, UTType.jpeg.identifier as CFString, 1, nil)
        else { throw Failure.message("Cannot convert \(src.lastPathComponent)") }
        CGImageDestinationAddImage(dest, image, [kCGImageDestinationLossyCompressionQuality: quality] as CFDictionary)
        guard CGImageDestinationFinalize(dest) else { throw Failure.message("Cannot write \(dst.lastPathComponent)") }
        return (image.width, image.height)
    }
}
