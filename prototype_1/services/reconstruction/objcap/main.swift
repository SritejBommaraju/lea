// lea-objcap: photos folder -> mesh, using Apple Object Capture (RealityKit PhotogrammetrySession).
// Usage: lea-objcap <images-dir> <output-dir> [preview|reduced|medium|full|raw]
//        lea-objcap --supported
// Writes <output-dir>/model.usdz and an OBJ bundle in <output-dir>/obj/, then prints one JSON line
// per progress event and a final {"done": ...} line. Units are whatever the photos imply; scale is
// unverified until the QA step applies a measured anchor.
import Foundation
import RealityKit

func emit(_ object: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: object), let line = String(data: data, encoding: .utf8) {
        print(line)
        fflush(stdout)
    }
}

let args = CommandLine.arguments
if args.count == 2 && args[1] == "--supported" {
    emit(["supported": PhotogrammetrySession.isSupported])
    exit(PhotogrammetrySession.isSupported ? 0 : 1)
}
guard args.count >= 3 else {
    FileHandle.standardError.write("usage: lea-objcap <images-dir> <output-dir> [preview|reduced|medium|full|raw]\n".data(using: .utf8)!)
    exit(64)
}
guard PhotogrammetrySession.isSupported else {
    emit(["error": "PhotogrammetrySession is not supported on this Mac"])
    exit(69)
}

let input = URL(fileURLWithPath: args[1], isDirectory: true)
let output = URL(fileURLWithPath: args[2], isDirectory: true)
let details: [String: PhotogrammetrySession.Request.Detail] = ["preview": .preview, "reduced": .reduced, "medium": .medium, "full": .full, "raw": .raw]
let detail = details[args.count > 3 ? args[3] : "medium"] ?? .medium
let objDir = output.appendingPathComponent("obj", isDirectory: true)
try FileManager.default.createDirectory(at: objDir, withIntermediateDirectories: true)

var configuration = PhotogrammetrySession.Configuration()
configuration.featureSensitivity = .normal
configuration.sampleOrdering = .sequential
configuration.isObjectMaskingEnabled = true

let session: PhotogrammetrySession
do {
    session = try PhotogrammetrySession(input: input, configuration: configuration)
} catch {
    emit(["error": "could not open the photo set: \(error.localizedDescription)"])
    exit(65)
}

let started = Date()
let usdz = output.appendingPathComponent("model.usdz")
var failures: [String] = []

let task = Task {
    for try await message in session.outputs {
        switch message {
        case .requestProgress(_, let fraction):
            emit(["progress": fraction])
        case .requestComplete(let request, let result):
            if case .modelFile(let url) = result { emit(["file": url.path, "request": "\(request)"]) }
        case .requestError(let request, let error):
            failures.append("\(request): \(error.localizedDescription)")
            emit(["requestError": error.localizedDescription])
        case .invalidSample(let id, let reason):
            emit(["invalidSample": id, "reason": reason])
        case .skippedSample(let id):
            emit(["skippedSample": id])
        case .processingComplete:
            emit(["done": failures.isEmpty, "seconds": Date().timeIntervalSince(started), "usdz": usdz.path,
                  "objDir": objDir.path, "detail": "\(detail)", "errors": failures])
            exit(failures.isEmpty ? 0 : 70)
        case .processingCancelled:
            emit(["done": false, "errors": ["cancelled"]])
            exit(70)
        default:
            break
        }
    }
}

do {
    try session.process(requests: [
        .modelFile(url: usdz, detail: detail),
        .modelFile(url: objDir, detail: detail),  // a directory URL produces OBJ + MTL + textures
    ])
} catch {
    emit(["error": "processing did not start: \(error.localizedDescription)"])
    exit(70)
}
_ = task
RunLoop.main.run()
