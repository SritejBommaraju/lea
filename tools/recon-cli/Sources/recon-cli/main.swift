// recon-cli: Apple PhotogrammetrySession on the Mac, for the RepairOS backend (backend/repairos/recon).
//
//   recon-cli samples     --input <images dir> --out <dir> [--depth]
//   recon-cli reconstruct --input <images dir> --out <dir> [--detail none|preview|reduced|medium|full]
//                         [--masking on|off] [--ordering unordered|sequential] [--sensitivity normal|high]
//                         [--no-depth] [--no-points] [--checkpoint <dir>]
//   recon-cli usdz2ply    --input <model.usdz> --out <mesh.ply>
//
// stdout: one JSON object, the result summary. stderr: JSON lines, one per progress event or error
// ({"event": ..., "t_s": seconds since start, ...}). Exit 0 on success, 1 on a processing failure, 2 on bad usage.
//
// Every API used here was checked against the macOS 27.2 SDK swiftinterface (RealityFoundation, arm64e-apple-macos):
// PhotogrammetrySession(input:configuration:) for a folder or a Sequence of PhotogrammetrySample;
// Configuration.{isObjectMaskingEnabled, sampleOrdering, featureSensitivity, checkpointDirectory};
// Request.{poses, pointCloud, modelFile(url:detail:geometry:)}; Poses.{posesBySample, urlsBySample};
// Pose.{translation, rotation, transform, intrinsics (macOS 26)}; PointCloud.points[{position, color}];
// PhotogrammetrySample(contentsOf:) (macOS 15) with {image, depthDataMap, depthConfidenceMap, gravity, camera
// {intrinsics, transform, calibrationData}, scanPassID, sessionID, boundingBox, captureTime, orientation (macOS 26)}.

import CoreMotion
import CoreVideo
import Foundation
import ModelIO
import RealityKit
import simd

// MARK: - Output plumbing

let startedNs = DispatchTime.now().uptimeNanoseconds   // monotonic: wall-clock changes cannot skew timings

func elapsed() -> Double { (Double(DispatchTime.now().uptimeNanoseconds - startedNs) / 1e6).rounded() / 1000 }

func jsonData(_ obj: Any) -> Data {
    (try? JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys])) ?? Data("{}".utf8)
}

func emit(_ event: String, _ fields: [String: Any] = [:]) {
    var d = fields
    d["event"] = event
    d["t_s"] = elapsed()
    var line = jsonData(d)
    line.append(0x0A)
    FileHandle.standardError.write(line)
}

func finish(_ summary: [String: Any], code: Int32) -> Never {
    var s = summary
    s["wall_s"] = elapsed()
    s["peak_rss_mb"] = peakRSSMB()
    var out = jsonData(s)
    out.append(0x0A)
    FileHandle.standardOutput.write(out)
    exit(code)
}

func fail(_ message: String, code: Int32 = 1) -> Never {
    emit("error", ["message": message])
    finish(["ok": false, "error": message], code: code)
}

func peakRSSMB() -> Double {
    var u = rusage()
    getrusage(RUSAGE_SELF, &u)
    return (Double(u.ru_maxrss) / 1_048_576 * 10).rounded() / 10   // macOS: bytes
}

func rowMajor(_ m: simd_float4x4) -> [Double] {
    (0..<4).flatMap { r in (0..<4).map { c in Double(m[c][r]) } }
}

func rowMajor(_ m: simd_float3x3) -> [Double] {
    (0..<3).flatMap { r in (0..<3).map { c in Double(m[c][r]) } }
}

func quatXYZW(_ q: simd_quatf) -> [Double] { [Double(q.imag.x), Double(q.imag.y), Double(q.imag.z), Double(q.real)] }

// MARK: - Arguments

struct Args {
    var command = ""
    var values: [String: String] = [:]
    var flags: Set<String> = []

    static let valued: Set<String> = ["--input", "--out", "--detail", "--masking", "--ordering", "--sensitivity",
                                      "--checkpoint"]
    static let boolean: Set<String> = ["--depth", "--no-depth", "--no-points"]

    init(_ argv: [String]) throws {
        guard let cmd = argv.first else { throw UsageError("missing command") }
        command = cmd
        var i = 1
        while i < argv.count {
            let a = argv[i]
            if Args.valued.contains(a) {
                guard i + 1 < argv.count else { throw UsageError("\(a) needs a value") }
                values[a] = argv[i + 1]
                i += 2
            } else if Args.boolean.contains(a) {
                flags.insert(a)
                i += 1
            } else {
                throw UsageError("unknown argument \(a)")
            }
        }
    }

    func url(_ key: String) throws -> URL {
        guard let v = values[key] else { throw UsageError("\(key) is required") }
        return URL(fileURLWithPath: (v as NSString).expandingTildeInPath)
    }
}

struct UsageError: Error { let message: String; init(_ m: String) { message = m } }

let usage = """
usage: recon-cli samples --input <dir> --out <dir> [--depth]
       recon-cli reconstruct --input <dir> --out <dir> [--detail none|preview|reduced|medium|full] \
[--masking on|off] [--ordering unordered|sequential] [--sensitivity normal|high] [--no-depth] [--no-points] \
[--checkpoint <dir>]
       recon-cli usdz2ply --input <model.usdz> --out <mesh.ply>
"""

// MARK: - Image listing

let imageExtensions: Set<String> = ["heic", "heif", "jpg", "jpeg", "png"]

func listImages(_ dir: URL) throws -> [URL] {
    let items = try FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil)
    return items.filter { imageExtensions.contains($0.pathExtension.lowercased()) }
        .sorted { $0.lastPathComponent < $1.lastPathComponent }
}

// MARK: - Pixel buffers

/// A one-channel pixel buffer as Float32 values (depth in metres, or confidence 0-2), row by row, plus its format.
func floatPlane(_ pb: CVPixelBuffer) -> (values: [Float], width: Int, height: Int, format: String)? {
    let fmt = CVPixelBufferGetPixelFormatType(pb)
    let w = CVPixelBufferGetWidth(pb), h = CVPixelBufferGetHeight(pb)
    CVPixelBufferLockBaseAddress(pb, .readOnly)
    defer { CVPixelBufferUnlockBaseAddress(pb, .readOnly) }
    guard let base = CVPixelBufferGetBaseAddress(pb) else { return nil }
    let bpr = CVPixelBufferGetBytesPerRow(pb)
    var out = [Float](repeating: 0, count: w * h)
    switch fmt {
    case kCVPixelFormatType_DepthFloat32, kCVPixelFormatType_DisparityFloat32, kCVPixelFormatType_OneComponent32Float:
        for y in 0..<h {
            let row = (base + y * bpr).assumingMemoryBound(to: Float.self)
            for x in 0..<w { out[y * w + x] = row[x] }
        }
    case kCVPixelFormatType_DepthFloat16, kCVPixelFormatType_DisparityFloat16, kCVPixelFormatType_OneComponent16Half:
        for y in 0..<h {
            let row = (base + y * bpr).assumingMemoryBound(to: Float16.self)
            for x in 0..<w { out[y * w + x] = Float(row[x]) }
        }
    case kCVPixelFormatType_OneComponent8:
        for y in 0..<h {
            let row = (base + y * bpr).assumingMemoryBound(to: UInt8.self)
            for x in 0..<w { out[y * w + x] = Float(row[x]) }
        }
    default:
        return nil
    }
    let names: [OSType: String] = [
        kCVPixelFormatType_DepthFloat32: "depth_f32", kCVPixelFormatType_DisparityFloat32: "disparity_f32",
        kCVPixelFormatType_DepthFloat16: "depth_f16", kCVPixelFormatType_DisparityFloat16: "disparity_f16",
        kCVPixelFormatType_OneComponent8: "u8", kCVPixelFormatType_OneComponent32Float: "f32",
        kCVPixelFormatType_OneComponent16Half: "f16",
    ]
    return (out, w, h, names[fmt] ?? "\(fmt)")
}

func writeFloats(_ values: [Float], to url: URL) throws {
    try values.withUnsafeBufferPointer { Data(buffer: $0) }.write(to: url)   // little-endian on Apple silicon
}

// MARK: - PLY

func writePointsPLY(_ points: [PhotogrammetrySession.PointCloud.Point], to url: URL) throws {
    var d = Data("""
    ply
    format binary_little_endian 1.0
    element vertex \(points.count)
    property float x
    property float y
    property float z
    property uchar red
    property uchar green
    property uchar blue
    end_header

    """.utf8)
    d.reserveCapacity(d.count + points.count * 15)
    for p in points {
        var xyz = [p.position.x, p.position.y, p.position.z]
        d.append(Data(bytes: &xyz, count: 12))
        d.append(contentsOf: [p.color.x, p.color.y, p.color.z])
    }
    try d.write(to: url)
}

func writeMeshPLY(vertices: [SIMD3<Float>], faces: [SIMD3<UInt32>], to url: URL) throws {
    var d = Data("""
    ply
    format binary_little_endian 1.0
    element vertex \(vertices.count)
    property float x
    property float y
    property float z
    element face \(faces.count)
    property list uchar int vertex_indices
    end_header

    """.utf8)
    d.reserveCapacity(d.count + vertices.count * 12 + faces.count * 13)
    for v in vertices {
        var xyz = [v.x, v.y, v.z]
        d.append(Data(bytes: &xyz, count: 12))
    }
    for f in faces {
        d.append(3)
        var idx = [Int32(f.x), Int32(f.y), Int32(f.z)]
        d.append(Data(bytes: &idx, count: 12))
    }
    try d.write(to: url)
}

/// Every triangle mesh in a USDZ, in the asset's root frame (global transforms applied), as one PLY.
func usdzToPLY(_ input: URL, _ output: URL) throws -> [String: Any] {
    guard MDLAsset.canImportFileExtension(input.pathExtension) else {
        throw UsageError("ModelIO cannot import .\(input.pathExtension)")
    }
    let asset = MDLAsset(url: input)
    var vertices: [SIMD3<Float>] = []
    var faces: [SIMD3<UInt32>] = []
    var meshes = 0
    for case let mesh as MDLMesh in asset.childObjects(of: MDLMesh.self) {
        guard let pos = mesh.vertexAttributeData(forAttributeNamed: MDLVertexAttributePosition, as: .float3) else {
            continue
        }
        meshes += 1
        let m = MDLTransform.globalTransform(with: mesh, atTime: 0)
        let base = UInt32(vertices.count)
        for i in 0..<mesh.vertexCount {
            let p = (pos.dataStart + i * pos.stride).assumingMemoryBound(to: Float.self)
            let w = m * SIMD4<Float>(p[0], p[1], p[2], 1)
            vertices.append(SIMD3(w.x, w.y, w.z))
        }
        for case let sub as MDLSubmesh in mesh.submeshes ?? [] where sub.geometryType == .triangles {
            let buf = sub.indexBuffer(asIndexType: .uInt32)
            let ptr = buf.map().bytes.assumingMemoryBound(to: UInt32.self)
            for t in 0..<(sub.indexCount / 3) {
                faces.append(SIMD3(ptr[3 * t] + base, ptr[3 * t + 1] + base, ptr[3 * t + 2] + base))
            }
        }
    }
    try writeMeshPLY(vertices: vertices, faces: faces, to: output)
    return ["meshes": meshes, "vertices": vertices.count, "triangles": faces.count, "file": output.path]
}

// MARK: - samples: what each image carries (ARKit pose, intrinsics, depth, gravity, scan pass)

func runSamples(_ args: Args) throws -> Never {
    let input = try args.url("--input"), out = try args.url("--out")
    let dumpDepth = args.flags.contains("--depth")
    try FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)
    let urls = try listImages(input)
    guard !urls.isEmpty else { fail("no images in \(input.path)") }
    var rows: [[String: Any]] = []
    for (i, url) in urls.enumerated() {
        autoreleasepool {
            var row: [String: Any] = ["index": i, "file": url.lastPathComponent]
            do {
                let s = try PhotogrammetrySample(contentsOf: url)
                row["width_px"] = CVPixelBufferGetWidth(s.image)
                row["height_px"] = CVPixelBufferGetHeight(s.image)
                row["orientation"] = Int(s.orientation.rawValue)
                if let t = s.captureTime { row["capture_time"] = t.timeIntervalSince1970 }
                if let g = s.gravity { row["gravity"] = [g.x, g.y, g.z] }
                if let p = s.scanPassID { row["scan_pass_id"] = p }
                if let sid = s.sessionID { row["session_uuid"] = sid.uuidString }
                if let b = s.boundingBox { row["bounding_box_row_major"] = rowMajor(b) }
                if let c = s.camera {
                    var cam: [String: Any] = ["intrinsics_row_major": rowMajor(c.intrinsics),
                                              "transform_row_major": rowMajor(c.transform), "camera_id": Int(c.id)]
                    if let cal = c.calibrationData {
                        let ref = cal.intrinsicMatrixReferenceDimensions
                        cam["calibration_reference_px"] = [Double(ref.width), Double(ref.height)]
                        cam["calibration_intrinsics_row_major"] = rowMajor(cal.intrinsicMatrix)
                    }
                    row["camera"] = cam
                }
                if let d = s.depthDataMap, let plane = floatPlane(d) {
                    var depth: [String: Any] = ["width": plane.width, "height": plane.height, "format": plane.format]
                    if dumpDepth {
                        let f = out.appending(path: "\(url.deletingPathExtension().lastPathComponent)_depth.f32")
                        try writeFloats(plane.values, to: f)
                        depth["file"] = f.lastPathComponent
                    }
                    row["depth"] = depth
                }
                if let c = s.depthConfidenceMap, let plane = floatPlane(c) {
                    var conf: [String: Any] = ["width": plane.width, "height": plane.height, "format": plane.format]
                    if dumpDepth {
                        let f = out.appending(path: "\(url.deletingPathExtension().lastPathComponent)_conf.f32")
                        try writeFloats(plane.values, to: f)
                        conf["file"] = f.lastPathComponent
                    }
                    row["confidence"] = conf
                }
                row["has_object_mask"] = s.objectMask != nil
            } catch {
                row["error"] = error.localizedDescription
                emit("sample_error", ["file": url.lastPathComponent, "message": error.localizedDescription])
            }
            rows.append(row)
            emit("sample", ["index": i, "of": urls.count])
        }
    }
    let file = out.appending(path: "samples.json")
    try jsonData(["images": rows, "input": input.path]).write(to: file)
    finish(["ok": true, "command": "samples", "images": urls.count, "samples_file": file.path,
            "with_camera": rows.filter { $0["camera"] != nil }.count,
            "with_depth": rows.filter { $0["depth"] != nil }.count], code: 0)
}

// MARK: - reconstruct

func detail(_ s: String) throws -> PhotogrammetrySession.Request.Detail? {
    switch s {
    case "none": return nil
    case "preview": return .preview
    case "reduced": return .reduced
    case "medium": return .medium
    case "full": return .full
    default: throw UsageError("--detail: none, preview, reduced, medium or full")
    }
}

func stageName(_ s: PhotogrammetrySession.Output.ProcessingStage?) -> String {
    guard let s else { return "" }
    switch s {
    case .preProcessing: return "preProcessing"
    case .imageAlignment: return "imageAlignment"
    case .pointCloudGeneration: return "pointCloudGeneration"
    case .meshGeneration: return "meshGeneration"
    case .textureMapping: return "textureMapping"
    case .optimization: return "optimization"
    @unknown default: return "\(s)"
    }
}

func requestName(_ r: PhotogrammetrySession.Request) -> String {
    switch r {
    case .poses: return "poses"
    case .pointCloud: return "pointCloud"
    case .modelFile: return "modelFile"
    case .modelEntity: return "modelEntity"
    case .bounds: return "bounds"
    @unknown default: return "\(r)"
    }
}

/// Samples without their depth (to test the no-LiDAR path on LiDAR captures): only image, gravity and object
/// mask are copied; the sample id is the image's index in `urls`. Built lazily, one image at a time.
func depthlessSamples(_ urls: [URL]) -> some Sequence<PhotogrammetrySample> {
    urls.enumerated().lazy.compactMap { (i, url) -> PhotogrammetrySample? in
        guard let s = try? PhotogrammetrySample(contentsOf: url) else {
            emit("sample_error", ["file": url.lastPathComponent, "message": "could not read"])
            return nil
        }
        var n = PhotogrammetrySample(id: i, image: s.image)
        n.gravity = s.gravity
        n.objectMask = s.objectMask
        return n
    }
}

func runReconstruct(_ args: Args) async throws -> Never {
    let input = try args.url("--input"), out = try args.url("--out")
    let det = try detail(args.values["--detail"] ?? "reduced")
    guard PhotogrammetrySession.isSupported else { fail("PhotogrammetrySession is not supported on this Mac") }
    try FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)

    var config = PhotogrammetrySession.Configuration()
    if let ck = args.values["--checkpoint"] { config.checkpointDirectory = URL(fileURLWithPath: ck) }
    switch args.values["--masking"] ?? "on" {
    case "on": config.isObjectMaskingEnabled = true
    case "off": config.isObjectMaskingEnabled = false
    default: throw UsageError("--masking: on or off")
    }
    switch args.values["--ordering"] ?? "unordered" {
    case "unordered": config.sampleOrdering = .unordered
    case "sequential": config.sampleOrdering = .sequential
    default: throw UsageError("--ordering: unordered or sequential")
    }
    switch args.values["--sensitivity"] ?? "normal" {
    case "normal": config.featureSensitivity = .normal
    case "high": config.featureSensitivity = .high
    default: throw UsageError("--sensitivity: normal or high")
    }

    let urls = try listImages(input)
    guard !urls.isEmpty else { fail("no images in \(input.path)") }
    let noDepth = args.flags.contains("--no-depth")
    let session: PhotogrammetrySession
    if noDepth {
        session = try PhotogrammetrySession(input: depthlessSamples(urls), configuration: config)
    } else {
        session = try PhotogrammetrySession(input: input, configuration: config)
    }

    var requests: [PhotogrammetrySession.Request] = [.poses]
    if !args.flags.contains("--no-points") { requests.append(.pointCloud) }
    let modelURL = out.appending(path: "model.usdz")
    if let det { requests.append(.modelFile(url: modelURL, detail: det)) }
    emit("start", ["images": urls.count, "requests": requests.map(requestName),
                   "limits": ["max_images": PhotogrammetrySession.limits.maximumNumberOfInputImages,
                              "max_dimension": PhotogrammetrySession.limits.maximumInputImageDimension],
                   "masking": config.isObjectMaskingEnabled, "no_depth": noDepth])
    try session.process(requests: requests)

    var summary: [String: Any] = ["ok": true, "command": "reconstruct", "images": urls.count, "no_depth": noDepth,
                                  "detail": args.values["--detail"] ?? "reduced",
                                  "masking": config.isObjectMaskingEnabled,
                                  "ordering": args.values["--ordering"] ?? "unordered",
                                  "sensitivity": args.values["--sensitivity"] ?? "normal"]
    var completed: [String: Double] = [:]
    var invalid: [[String: Any]] = [], skipped: [Int] = []
    var errors: [String] = []
    var stitchingIncomplete = false, downsampled = false
    var lastStage = ""
    var lastFraction: [String: Double] = [:]

    outputs: for try await output in session.outputs {
        switch output {
        case .requestProgress(let r, let f):
            let name = requestName(r)
            if f - (lastFraction[name] ?? -1) >= 0.05 || f >= 1 {
                lastFraction[name] = f
                emit("progress", ["request": name, "fraction": (f * 1000).rounded() / 1000])
            }
        case .requestProgressInfo(let r, let info):
            let stage = stageName(info.processingStage)
            if stage != lastStage {
                lastStage = stage
                emit("stage", ["request": requestName(r), "stage": stage, "eta_s": info.estimatedRemainingTime ?? -1])
            }
        case .requestComplete(let r, let result):
            completed[requestName(r)] = elapsed()
            switch result {
            case .poses(let poses):
                let names = poses.urlsBySample
                var rows: [[String: Any]] = []
                for (id, p) in poses.posesBySample.sorted(by: { $0.key < $1.key }) {
                    var row: [String: Any] = ["id": id, "translation": [p.translation.x, p.translation.y,
                                                                        p.translation.z].map(Double.init),
                                              "quaternion_xyzw": quatXYZW(p.rotation),
                                              "transform_row_major": rowMajor(p.transform.matrix)]
                    if let u = names[id] { row["file"] = u.lastPathComponent }
                    else if noDepth, id < urls.count { row["file"] = urls[id].lastPathComponent }
                    if let k = p.intrinsics { row["intrinsics_row_major"] = rowMajor(k) }
                    rows.append(row)
                }
                let f = out.appending(path: "poses.json")
                try jsonData(["poses": rows, "images": urls.map(\.lastPathComponent)]).write(to: f)
                summary["poses"] = rows.count
                summary["poses_file"] = f.path
                emit("poses", ["count": rows.count])
            case .pointCloud(let cloud):
                let f = out.appending(path: "points.ply")
                try writePointsPLY(cloud.points, to: f)
                summary["points"] = cloud.points.count
                summary["points_file"] = f.path
                emit("point_cloud", ["count": cloud.points.count])
            case .modelFile(let url):
                summary["model_file"] = url.path
                emit("model_file", ["file": url.lastPathComponent])
            default:
                break
            }
        case .requestError(let r, let error):
            errors.append("\(requestName(r)): \(error.localizedDescription)")
            emit("request_error", ["request": requestName(r), "message": error.localizedDescription])
        case .invalidSample(let id, let reason):
            invalid.append(["id": id, "reason": reason])
            emit("invalid_sample", ["id": id, "reason": reason])
        case .skippedSample(let id):
            skipped.append(id)
            emit("skipped_sample", ["id": id])
        case .automaticDownsampling:
            downsampled = true
            emit("automatic_downsampling")
        case .stitchingIncomplete:
            stitchingIncomplete = true
            emit("stitching_incomplete")
        case .inputComplete:
            emit("input_complete")
        case .processingComplete:
            emit("processing_complete")
            break outputs
        case .processingCancelled:
            emit("processing_cancelled")
            errors.append("cancelled")
            break outputs
        @unknown default:
            emit("unknown_output", ["output": "\(output)"])
        }
    }

    if let model = summary["model_file"] as? String {
        let meshURL = out.appending(path: "mesh.ply")
        do {
            let info = try usdzToPLY(URL(fileURLWithPath: model), meshURL)
            summary["mesh_file"] = meshURL.path
            summary["mesh_triangles"] = info["triangles"]
            summary["mesh_vertices"] = info["vertices"]
        } catch {
            errors.append("usdz2ply: \(error)")
        }
    }
    summary["request_done_s"] = completed
    summary["invalid_samples"] = invalid
    summary["skipped_samples"] = skipped
    summary["stitching_incomplete"] = stitchingIncomplete
    summary["automatic_downsampling"] = downsampled
    summary["errors"] = errors
    let ok = summary["poses"] != nil
    summary["ok"] = ok
    finish(summary, code: ok ? 0 : 1)
}

// MARK: - main

let argv = Array(CommandLine.arguments.dropFirst())
do {
    let args = try Args(argv)
    switch args.command {
    case "samples": try runSamples(args)
    case "reconstruct": try await runReconstruct(args)
    case "usdz2ply":
        let info = try usdzToPLY(try args.url("--input"), try args.url("--out"))
        finish(["ok": true, "command": "usdz2ply"].merging(info) { a, _ in a }, code: 0)
    case "-h", "--help", "help":
        print(usage)
        exit(0)
    default:
        throw UsageError("unknown command \(args.command)")
    }
} catch let e as UsageError {
    FileHandle.standardError.write(Data((usage + "\n").utf8))
    fail(e.message, code: 2)
} catch {
    fail("\(error)")
}
