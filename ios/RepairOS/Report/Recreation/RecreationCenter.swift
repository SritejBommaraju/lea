import Foundation
import Observation

// Where the scan's recreation comes from and when it changes:
//   Tier 1, the phone's textured shell: built once per scan right after Done (ShellBuilder, a background utility
//   task, one at a time; the capture has ended, so it never competes with the 60 fps preview), cached in
//   `<scanDir>/recreation/phone/`;
//   Tier 2, the gateway's recreation: fetched on the live `recreation_ready` event (or when the report opens) from
//   GET /v1/scans/{id}/recreation, cached in `<scanDir>/recreation/gateway/`, and preferred over Tier 1 once there.
// Both are in the scan's ARKit frame, the frame of the findings' `location.point_m`.

@MainActor
@Observable
final class RecreationCenter {
    static let shared = RecreationCenter()

    enum State: Equatable { case none, building, ready, failed(String) }

    /// Per scan folder (its path): what Tier 1 is doing, and a counter bumped whenever new files land there (a view
    /// keyed on it reloads).
    private(set) var shell: [String: State] = [:]
    private(set) var version: [String: Int] = [:]
    private var queue: [(sessionDir: URL, scanID: String)] = []
    private var running = false
    private var fetching: Set<String> = []

    static func scanDir(sessionDir: URL, scanID: String) -> URL { sessionDir.appending(path: "scans/\(scanID)") }

    /// The best recreation in a scan folder: the gateway's when it is one this build can draw, else the phone's.
    nonisolated static func best(in scanDir: URL) -> (manifest: RecreationManifest, folder: URL)? {
        for sub in ["recreation/gateway", "recreation/phone"] {
            let folder = scanDir.appending(path: sub)
            guard let data = try? Data(contentsOf: folder.appending(path: "recreation.json")),
                  let m = try? JSONDecoder().decode(RecreationManifest.self, from: data),
                  FileManager.default.fileExists(atPath: folder.appending(path: m.file).path) else { continue }
            if RecreationView.canDraw(m) { return (m, folder) }
        }
        return nil
    }

    func available(_ scanDir: URL) -> Bool {
        _ = version[scanDir.path]
        return Self.best(in: scanDir) != nil
    }

    // MARK: Tier 1

    /// Queues the phone's shell for a scan (after Done; again after "Continue scanning", which re-saves the package).
    func buildShell(sessionDir: URL, scanID: String, force: Bool = false) {
        let dir = Self.scanDir(sessionDir: sessionDir, scanID: scanID)
        if !force, FileManager.default.fileExists(atPath: dir.appending(path: "\(ShellBuilder.folder)/recreation.json").path) { return }
        guard !queue.contains(where: { $0.sessionDir == sessionDir && $0.scanID == scanID }) else { return }
        queue.append((sessionDir, scanID))
        shell[dir.path] = .building
        runNext()
    }

    private func runNext() {
        guard !running, !queue.isEmpty else { return }
        running = true
        let job = queue.removeFirst()
        let dir = Self.scanDir(sessionDir: job.sessionDir, scanID: job.scanID)
        let diag = Diag.session(job.sessionDir)
        diag.log("recreation_build_start", ["scan_id": job.scanID])
        Task.detached(priority: .utility) {
            let result: Result<RecreationManifest, Error>
            do { result = .success(try ShellBuilder.build(sessionDir: job.sessionDir, scanID: job.scanID)) } catch { result = .failure(error) }
            await MainActor.run {
                switch result {
                case .success(let m):
                    diag.log("recreation_built", m.stats.reduce(into: ["scan_id": job.scanID]) { $0[$1.key] = ($1.value * 1000).rounded() / 1000 })
                    self.shell[dir.path] = .ready
                case .failure(let e):
                    diag.log("recreation_failed", ["scan_id": job.scanID, "error": "\(e)"])
                    self.shell[dir.path] = .failed("\(e)")
                }
                self.version[dir.path, default: 0] += 1
                self.running = false
                self.runNext()
            }
        }
    }

    // MARK: Tier 2

    /// Fetches the gateway's recreation when it is newer than the cached one (`rev` from `recreation_ready`; nil =
    /// ask the gateway what it has).
    func fetchGateway(_ gateway: GatewayClient, sessionDir: URL, scanID: String, rev: Int?) async {
        let dir = Self.scanDir(sessionDir: sessionDir, scanID: scanID)
        let folder = dir.appending(path: "recreation/gateway")
        let have = (try? Data(contentsOf: folder.appending(path: "recreation.json")))
            .flatMap { try? JSONDecoder().decode(RecreationManifest.self, from: $0) }
        if let rev, let have, have.rev >= rev { return }
        guard !fetching.contains(dir.path) else { return }
        fetching.insert(dir.path)
        defer { fetching.remove(dir.path) }
        let started = Date()
        guard let got = try? await gateway.recreation(scanID: scanID), got.rev > (have?.rev ?? 0) || have == nil else { return }
        let ext = got.format.isEmpty ? "bin" : got.format
        let manifest = RecreationManifest(kind: got.kind, origin: "gateway", rev: got.rev, file: "model.\(ext)", texture: nil,
                                          focusCenter: got.focusCenter, focusRadius: got.focusRadius,
                                          stats: ["bytes": Double(got.data.count), "fetch_s": Date().timeIntervalSince(started),
                                                  RecreationManifest.bottomKey: Double(got.bottomSegments)])
        do {
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            try got.data.write(to: folder.appending(path: manifest.file), options: .atomic)
            try JSONEncoder().encode(manifest).write(to: folder.appending(path: "recreation.json"), options: .atomic)
        } catch {
            return
        }
        Diag.session(sessionDir).log("recreation_fetched", ["scan_id": scanID, "kind": got.kind, "rev": got.rev,
                                                            "kb": got.data.count / 1024, "drawable": RecreationView.canDraw(manifest)])
        version[dir.path, default: 0] += 1
    }
}
