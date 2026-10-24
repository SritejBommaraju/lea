import Foundation
import Testing
@testable import RepairOS

struct ContractTests {
    /// The package the app writes for a no-LiDAR scan: no points/mesh fields, ISO dates, no nulls. The JSON is
    /// also left in the temp folder as `repairos_scan_package_test.json` for `spec/contracts/check.py`.
    @Test func noLidarPackageEncodesWithoutGeometryFields() throws {
        let kf = ScanPackageSchema(
            camera: KeyframeCamera(frame: .arkitWorld, positionM: [0, 0, 0], quaternionXyzw: [0, 0, 0, 1]),
            confidenceFile: nil, depthEncoding: nil, depthFile: nil, exposureS: 0.008, imageFile: "scans/scan_a/kf_000.jpg",
            intrinsics: KeyframeIntrinsics(cx: 960, cy: 720, fx: 1400, fy: 1400, heightPx: 1440, widthPx: 1920),
            keyframeID: "kf_000", segment: nil, sharpness: 120, timestampS: 0, trackingState: .normal)
        let pkg = ScanPackage(
            alignment: nil, capturedAt: Date(timeIntervalSince1970: 1_793_000_000), coverageHint: nil,
            cropBox: ObjectBox(center: SIMD3(0, 0.03, -0.5), size: SIMD3(0.06, 0.06, 0.06), yaw: 0.3).contract, depthSource: DepthSource.none,
            device: Device(appVersion: "repairos/0.1", ios: "26.5", model: "iPhone18,3"), frame: .arkitWorld,
            gravityArkitWorld: [0, -1, 0], keyframes: [kf], meshFile: nil, pointCount: nil, pointsFile: nil, poseSource: .arkit,
            productID: "prod_x", scanID: "scan_a", segments: nil, sessionID: "sess_local_x", supportPlane: nil, supportingPlaneRemoved: nil,
            symptom: nil, units: .m, voxelMm: nil)
        let data = try JSON.encoder.encode(pkg)
        let text = String(decoding: data, as: UTF8.self)
        #expect(!text.contains("null"))
        #expect(!text.contains("points_file"))
        #expect(text.contains("\"depth_source\":\"none\""))
        #expect(text.contains("\"captured_at\":\"2026-"))
        try data.write(to: FileManager.default.temporaryDirectory.appending(path: "repairos_scan_package_test.json"))
    }
}

struct GatewayClientTests {
    /// Runs against the real gateway when the test run is given one (TEST_RUNNER_REPAIROS_GATEWAY and
    /// TEST_RUNNER_REPAIROS_TOKEN); there is no stand-in server. Uploads half a blob, then resumes from the
    /// gateway's offset with a fresh client, as after a killed app.
    @Test(.enabled(if: ProcessInfo.processInfo.environment["REPAIROS_GATEWAY"] != nil))
    func blobUploadResumesFromGatewayOffset() async throws {
        let env = ProcessInfo.processInfo.environment
        let client = GatewayClient(baseURL: URL(string: env["REPAIROS_GATEWAY"]!)!, token: env["REPAIROS_TOKEN"])
        let bytes = Data((0..<(GatewayClient.chunkSize + 1000)).map { _ in UInt8.random(in: 0...255) })
        let url = FileManager.default.temporaryDirectory.appending(path: "\(UUID().uuidString).bin")
        try bytes.write(to: url)
        let sha = try GatewayClient.sha256(of: url)

        // First chunk only, as an interrupted upload leaves it.
        var req = URLRequest(url: URL(string: "\(env["REPAIROS_GATEWAY"]!)/v1/blobs/\(sha)")!)
        req.httpMethod = "PUT"
        req.setValue("Bearer \(env["REPAIROS_TOKEN"] ?? "")", forHTTPHeaderField: "Authorization")
        req.setValue("bytes 0-\(GatewayClient.chunkSize - 1)/\(bytes.count)", forHTTPHeaderField: "Content-Range")
        let (_, resp) = try await URLSession.shared.upload(for: req, from: bytes.prefix(GatewayClient.chunkSize))
        #expect((resp as? HTTPURLResponse)?.statusCode == 202)

        let ref = try await client.uploadBlob(file: url, sha256: sha, contentType: "application/octet-stream")
        #expect(ref == "blob://sha256:\(sha)")
        // Uploading again is a no-op that returns the same ref.
        #expect(try await client.uploadBlob(file: url, sha256: sha, contentType: "application/octet-stream") == ref)
    }
}
