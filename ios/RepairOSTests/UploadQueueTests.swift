import Foundation
import Testing
@testable import RepairOS

/// What the upload queue sends for a continued scan (Greptile, PR #7).
@MainActor
struct UploadQueueTests {
    /// "Continue scanning" (aligned) rewrites the merged cloud under the same name: its old ref must not be sent
    /// for it (the gateway refuses the package when the point counts differ, and keeps the old cloud when not).
    @Test func aRewrittenCloudIsUploadedAgain() throws {
        let url = FileManager.default.temporaryDirectory.appending(path: "repairos-\(UUID().uuidString).ply")
        try Data(repeating: 1, count: 100).write(to: url)
        let path = "scans/scan_x/points_arkit_world.ply"
        // As the first upload left it: this file's ref, made from this very file.
        var st = UploadQueue.State()
        st.refs[path] = "blob://sha256:first"
        st.stamps = [path: UploadQueue.stamp(of: url)]
        let saved = try JSON.decoder.decode(UploadQueue.State.self, from: JSON.encoder.encode(st))   // upload.json
        #expect(saved.cachedRef(path, stamp: UploadQueue.stamp(of: url)) == "blob://sha256:first")

        try Data(repeating: 2, count: 140).write(to: url)
        #expect(saved.cachedRef(path, stamp: UploadQueue.stamp(of: url)) == nil)   // uploaded again

        // An upload.json from before stamps were kept: its refs are not trusted for a file that may have changed.
        let old = try JSON.decoder.decode(UploadQueue.State.self, from: Data(#"{"refs":{"\#(path)":"blob://sha256:first"},"scanPosted":true,"stillsPosted":[],"completed":false}"#.utf8))
        #expect(old.cachedRef(path, stamp: UploadQueue.stamp(of: url)) == nil)
    }

    /// Voice finds a finished scan's gateway session in its saved upload state (the record never holds it).
    @Test func theGatewaySessionIsReadFromTheSavedUpload() throws {
        let storage = ScanStorage(root: FileManager.default.temporaryDirectory.appending(path: "repairos-tests-\(UUID().uuidString)"))
        let record = try storage.newSession(symptom: nil, engine: .arkit)
        #expect(UploadQueue.gatewaySessionID(of: record, in: storage) == nil)   // not uploaded yet
        var st = UploadQueue.State()
        st.gatewaySessionID = "sess_gateway_1"
        try JSON.encoder.encode(st).write(to: storage.dir(record).appending(path: "upload.json"))
        #expect(UploadQueue.gatewaySessionID(of: record, in: storage) == "sess_gateway_1")
        #expect(record.gatewaySessionID == nil)
    }

    /// An unaligned continuation writes its own cloud into `segments[].points_file`: it is uploaded and replaced by
    /// its blob ref like every other file field (the gateway refuses a local path there).
    @Test func everySegmentCloudBecomesABlobRef() async throws {
        let pkg: [String: Any] = [
            "points_file": "scans/x/points_arkit_world.ply",
            "keyframes": [["image_file": "scans/x/kf_000.jpg", "depth_file": "scans/x/kf_000_depth.png"]],
            "segments": [["segment": 1, "points_file": "scans/x/points_segment1_arkit_world.ply"], ["segment": 2]],
        ]
        let out = try await UploadQueue.withRefs(pkg) { v in (v as? String).map { "blob://\($0)" } }
        #expect(out["points_file"] as? String == "blob://scans/x/points_arkit_world.ply")
        let kfs = try #require(out["keyframes"] as? [[String: Any]])
        #expect(kfs[0]["depth_file"] as? String == "blob://scans/x/kf_000_depth.png")
        let segs = try #require(out["segments"] as? [[String: Any]])
        #expect(segs[0]["points_file"] as? String == "blob://scans/x/points_segment1_arkit_world.ply")
        #expect(segs[1]["points_file"] == nil)
        #expect(segs[1]["segment"] as? Int == 2)
    }
}
