import Foundation
import simd
import Testing
@testable import RepairOS

struct SSEParserTests {
    /// The gateway's framing (backend/repairos/api/app.py `events`): retry, then id/event/data blocks.
    @Test func parsesGatewayFraming() {
        let raw = "retry: 1000\n\nid: 7\nevent: run.update\ndata: {\"a\":1}\n\nid: 7\nevent: identified\ndata: {\"scan_id\":\"s\",\"category\":\"widget\"}\n\n: keepalive\n\n"
        var p = SSEParser()
        var out: [SSEParser.Event] = []
        for line in raw.components(separatedBy: "\n") { if let e = p.feed(line) { out.append(e) } }
        #expect(out == [SSEParser.Event(name: "run.update", data: "{\"a\":1}", id: "7"),
                        SSEParser.Event(name: "identified", data: "{\"scan_id\":\"s\",\"category\":\"widget\"}", id: "7")])
    }

    @Test func dispatchesWithoutBlankLines() {
        var p = SSEParser()
        #expect(p.feed("event: progress") == nil)
        #expect(p.feed("data: {}") == nil)
        #expect(p.feed("id: 9")?.name == "progress")
        #expect(p.feed("data: x") == nil)
        #expect(p.flush()?.data == "x")
    }

    @MainActor @Test func liveEventsKeepOnlyThisScan() {
        let live = LiveEvents(scanID: "scan_a")
        live.handle(.init(name: "identified", data: #"{"scan_id":"scan_b","category":"other"}"#, id: "1"))
        #expect(live.identity == nil)
        live.handle(.init(name: "identified", data: #"{"scan_id":"scan_a","category":"unknown"}"#, id: "2"))
        #expect(live.identity == nil)
        live.handle(.init(name: "identified", data: #"{"scan_id":"scan_a","category":"small_widget","brand":"Acme","model":null}"#, id: "3"))
        #expect(live.identity?.words == "Small widget · Acme")
        live.handle(.init(name: "progress", data: #"{"scan_id":"scan_a","areas_checked":4}"#, id: "4"))
        #expect(live.areasChecked == 4)
        live.handle(.init(name: "mesh_ready", data: #"{"scan_id":"scan_a","mesh_rev":2}"#, id: "5"))
        #expect(live.meshRev == 2)
        live.correct("  my thing ")
        #expect(live.corrected == "my thing")
    }
}

struct ObjectWordsTests {
    @Test func categoryThenBrandModel() {
        #expect(ReportModel.objectWords(["category": "widget", "brand": "Acme", "model": "X2"]) == "Widget · Acme X2")
        #expect(ReportModel.objectWords(["category": "widget"]) == "Widget")
        #expect(ReportModel.objectWords(["category": "unknown"]) == nil)
        #expect(ReportModel.objectWords(nil) == nil)
        #expect(ReportModel(json: ["object": ["category": "widget", "confidence_word": "low", "label_visible": false]]).objectWords == "Widget")
    }
}

struct MeshReaderTests {
    /// A binary PLY: two triangles on the object (above the box floor) and one on the table outside the box.
    static func meshPLY() -> Data {
        let verts: [SIMD3<Float>] = [[0, 0.02, 0], [0.01, 0.02, 0], [0, 0.03, 0], [0.01, 0.03, 0],
                                      [0.5, 0, 0.5], [0.6, 0, 0.5], [0.5, 0, 0.6]]
        var d = Data("ply\nformat binary_little_endian 1.0\nelement vertex \(verts.count)\nproperty float x\nproperty float y\nproperty float z\nproperty uchar red\nproperty uchar green\nproperty uchar blue\nelement face 3\nproperty list uchar int vertex_indices\nend_header\n".utf8)
        for v in verts {
            for c in [v.x, v.y, v.z] { withUnsafeBytes(of: c) { d.append(contentsOf: $0) } }
            d.append(contentsOf: [200, 100, 50])
        }
        for f: [Int32] in [[0, 1, 2], [1, 3, 2], [4, 5, 6]] {
            d.append(3)
            for i in f { withUnsafeBytes(of: i) { d.append(contentsOf: $0) } }
        }
        return d
    }

    @Test func readsTrianglesAndColours() throws {
        let mesh = try #require(try PLYReader.readMesh(Self.meshPLY()))
        #expect(mesh.positions.count == 7)
        #expect(mesh.indices.count == 9)
        #expect(mesh.colors?.first == SIMD3<Float>(200, 100, 50) / 255)
        #expect(abs(simd_length(mesh.normals[0]) - 1) < 1e-4)
    }

    @Test func cropKeepsTheObjectOnly() throws {
        let mesh = try #require(try PLYReader.readMesh(Self.meshPLY()))
        let box = ObjectBox(center: SIMD3(0.005, 0.025, 0), size: SIMD3(0.04, 0.05, 0.04), yaw: 0)
        let cropped = mesh.cropped(to: box, margin: 0.005, floor: 0.004)
        #expect(cropped.indices.count == 6)
        #expect(cropped.positions.count == 4)
        #expect(cropped.indices.allSatisfy { $0 < 4 })
    }

    @Test func pointCloudPLYHasNoFaces() throws {
        let url = FileManager.default.temporaryDirectory.appending(path: "cloud_\(UUID().uuidString).ply")
        _ = try PLYWriter.write([SIMD3(0, 0, 0), SIMD3(1, 0, 0)], colors: [SIMD3(1, 2, 3), SIMD3(4, 5, 6)], to: url)
        #expect(try PLYReader.readMesh(Data(contentsOf: url)) == nil)
    }
}

struct ReplayAndCueTests {
    @Test func coverageCellIDsRoundTrip() {
        for cell in 0..<CoverageDome.cellCount {
            #expect(ScanReplayView.cellIndex(CoverageDome.id(cell)) == cell)
        }
        #expect(ScanReplayView.cellIndex("r9s01") == nil)
        #expect(ScanReplayView.cellIndex("x") == nil)
    }

    /// Every cue the quality gate can give has its own symbol and urgency (no silent fallback).
    @Test func everyGateReasonIsStyled() {
        for reason in ["Keep the object in frame", "Too far: move closer", "Too close: back up", "Blurry: hold still",
                       "Move slowly", "Add light or move closer", "Too dark: add light"] {
            #expect(CueStyle.table[reason] != nil, "\(reason)")
        }
        #expect(CueStyle.look("Too close: back up").tone == .stop)
    }
}

struct ObjectAlignerTests {
    /// An L-shaped block (12 × 8 × 6 cm with a 6 cm tall arm): surface points, outward normals by face.
    static func block() -> [SIMD3<Float>] {
        var p: [SIMD3<Float>] = []
        func face(_ o: SIMD3<Float>, _ a: SIMD3<Float>, _ b: SIMD3<Float>) {
            for i in 0...24 { for j in 0...24 { p.append(o + a * Float(i) / 24 + b * Float(j) / 24) } }
        }
        // Base slab 12 × 3 × 8 cm on the table (y = 0), and an arm 4 × 6 × 8 cm on its left end.
        face([0, 0.03, 0], [0.12, 0, 0], [0, 0, 0.08])          // slab top
        face([0, 0, 0], [0, 0.03, 0], [0, 0, 0.08])             // left
        face([0.12, 0, 0], [0, 0.03, 0], [0, 0, 0.08])          // right
        face([0, 0, 0], [0.12, 0, 0], [0, 0.03, 0])             // front
        face([0, 0, 0.08], [0.12, 0, 0], [0, 0.03, 0])          // back
        face([0, 0.03, 0], [0.04, 0, 0], [0, 0.06, 0])          // arm front
        face([0, 0.03, 0.08], [0.04, 0, 0], [0, 0.06, 0])       // arm back
        face([0.04, 0.03, 0], [0, 0.06, 0], [0, 0, 0.08])       // arm right
        face([0, 0.09, 0], [0.04, 0, 0], [0, 0, 0.08])          // arm top
        return p
    }

    @Test func recoversYawAndOffsetFromOneSide() throws {
        let pts = Self.block()
        let target = try #require(AlignTarget.make(points: pts, colors: pts.map { _ in SIMD3(0.5, 0.4, 0.3) }, planeY: 0))
        let yaw: Float = 1.1, shift = SIMD3<Float>(0.4, 0.02, -0.3)
        let truth = ObjectAligner.matrix(aL: shift, aS: .zero, yaw: -yaw, trans: .zero)   // saved → live
        let centre = SIMD3<Float>(0.06, 0.04, 0.04)
        let camSaved = centre + simd_normalize(SIMD3<Float>(0.3, 0.6, 1)) * 0.4
        var live: [LivePoint] = []
        // Visible side only (faces toward the camera), plus the table.
        for (i, p) in pts.enumerated() where i % 2 == 0 {
            let n = target.normals[target.grid.nearest(p, maxDist: 0.01)?.index ?? 0]
            guard simd_dot(n, simd_normalize(camSaved - p)) > 0.1 else { continue }
            live.append(LivePoint(p: (truth * SIMD4(p, 1)).xyz, rgb: SIMD3(0.5, 0.4, 0.3)))
        }
        for i in 0..<3000 {
            let q = SIMD3<Float>(Float(i % 60) / 60 * 0.4 - 0.14, 0, Float(i / 60) / 50 * 0.4 - 0.16)
            live.append(LivePoint(p: (truth * SIMD4(q, 1)).xyz, rgb: SIMD3(0.3, 0.3, 0.3)))
        }
        let r = try #require(ObjectAligner.align(target: target, live: live, aim: (truth * SIMD4(centre, 1)).xyz,
                                                 planeY: shift.y, camera: (truth * SIMD4(camSaved, 1)).xyz))
        #expect(r.accepted, "refused: \(r.refused ?? "")")
        #expect(ObjectAligner.poseDistance(r.savedToLive, truth, at: centre, reach: 0.06) < 0.01)
    }

    @Test func refusesWithNoObject() {
        let pts = Self.block()
        let target = AlignTarget.make(points: pts, colors: pts.map { _ in .one }, planeY: 0)!
        let table = (0..<3000).map { i in LivePoint(p: SIMD3(Float(i % 60) / 100, 0, Float(i / 60) / 100), rgb: .one) }
        let r = ObjectAligner.align(target: target, live: table, aim: SIMD3(0.3, 0, 0.25), planeY: 0, camera: SIMD3(0.3, 0.4, 0.6))
        #expect(r == nil || r?.accepted == false)
    }
}
