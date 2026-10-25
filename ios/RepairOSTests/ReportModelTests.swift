import Foundation
import Testing
@testable import RepairOS

/// The report screen's reading of report JSON (spec/contracts/examples) and of the scan's PLY.
struct ReportModelTests {
    static let examples = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
        .appending(path: "../../spec/contracts/examples").standardized

    static func example(_ name: String) throws -> ReportModel {
        let data = try Data(contentsOf: examples.appending(path: name))
        return ReportModel(json: try #require(try JSONSerialization.jsonObject(with: data) as? [String: Any]))
    }

    @Test(arguments: ["report.json", "report.insufficient.json", "report.looks_good.json"])
    func examplesReadWithoutNumbersForConfidence(_ name: String) throws {
        let r = try Self.example(name)
        let shown = r.mostLikely + r.otherIssues + r.cosmetic
        for p in shown {
            #expect(!(p.confidence ?? "").contains("%"))
            #expect(p.confidence.map { $0.hasSuffix(" confidence") } ?? true)
        }
        for g in r.notInspected + r.insufficient { #expect(!g.reason.isEmpty) }
    }

    @Test func mostLikelyFollowsTheReportOrderAndSkipsNoIssue() throws {
        let r = try Self.example("report.json")
        #expect(r.verdict == .potentialIssues)
        #expect(r.mostLikely.map(\.id) == ["f_example_a"])
        #expect(r.mostLikely.first?.issue == "May be loose")
        #expect(r.mostLikely.first?.observation == "a gap is visible between the plug and its housing")
        #expect(r.otherIssues.isEmpty)
        #expect(r.notInspected.count == 2)
        #expect(r.insufficient.count == 1)
        #expect(r.nextStep?.title == "Take a closer photo")
    }

    @Test func locationOnlyWhenComputed() {
        #expect(ReportModel.point(["status": "computed", "point_m": [0.1, 0.02, -0.3]]) == SIMD3<Float>(0.1, 0.02, -0.3))
        #expect(ReportModel.point(["status": "not_computed", "reason": "no pose"]) == nil)
        #expect(ReportModel.point(["status": "computed", "point_m": [0.1, 0.2]]) == nil)
    }

    @Test func gapReasonDropsModelNameAndDetectorNoise() {
        let r = ReportModel(json: ["insufficient_evidence": [[
            "finding_id": "f1", "part_class": "plug prong",
            "reason": "could not judge the plug prong in still s11; vision model (some-model): The crop is blurry.; detector region labelled 'missing screw' (weak match)",
            "photo": ["still_id": "s11"],
        ]]])
        let g = try? #require(r.insufficient.first)
        #expect(g?.reason == "Could not judge the plug prong in still s11")
        #expect(g?.detail == "The crop is blurry.")
        #expect(g?.photoLabel == "Photo 11")
    }

    @Test func plyRoundTripAndCap() throws {
        let url = FileManager.default.temporaryDirectory.appending(path: "\(UUID().uuidString).ply")
        let pts = (0..<1000).map { SIMD3<Float>(Float($0), 1, -2) }
        try PLYWriter.write(pts, colors: pts.map { _ in SIMD3<UInt8>(255, 0, 128) }, to: url)
        let all = try PLYReader.read(try Data(contentsOf: url))
        #expect(all.positions.count == 1000)
        #expect(all.positions[10] == SIMD3(10, 1, -2))
        #expect(all.colors?.first == SIMD3<Float>(1, 0, Float(128) / 255))
        let capped = try PLYReader.read(try Data(contentsOf: url), maxPoints: 300)
        #expect(capped.positions.count <= 300)
        #expect(capped.positions.count >= 250)
    }
}

/// Assembly states (backend vision/assembly.py) read in plain words, never as raw state names.
struct AssemblyWordingTests {
    @Test func assemblyStatesReadPlainly() {
        #expect(ReportModel.issueWords("reversed") == "In the wrong way round")
        #expect(ReportModel.issueWords("unseated") == "Not plugged in fully")
        #expect(ReportModel.issueWords("missing") == "Looks missing")
        let m = ReportModel(json: ["verdict": "potential_issues", "most_likely": ["f1"], "not_inspected": [],
                                   "insufficient_evidence": [],
                                   "findings": [["finding_id": "f1", "part_class": "AA cell", "issue": "reversed"]]])
        #expect(m.mostLikely.first?.issue == "In the wrong way round")
    }

    /// Setup rows (report `setup`) are read apart with their fix, and a report with nothing broken leads with the fix.
    @Test func setupRowsAreListedApartAndLeadWhenNothingIsBroken() {
        let m = ReportModel(json: [
            "verdict": "no_problems_seen", "most_likely": [], "findings": [], "not_inspected": [],
            "insufficient_evidence": [],
            "setup": [["finding_id": "s1", "part_class": "battery bay", "issue": "missing", "category": "setup",
                       "fix": "Put the batteries in, then test again."]],
        ])
        #expect(m.setup.count == 1 && m.mostLikely.isEmpty && m.otherIssues.isEmpty)
        #expect(m.setup.first?.isSetup == true && m.setup.first?.fix == "Put the batteries in, then test again.")
        #expect(ReportWords.headline(m) == "Put the batteries in, then test again.")
        #expect(ReportWords.eyebrow(m).hasSuffix("1 thing to set up"))
    }

    static var battery: [String: Any] { [
        "finding_id": "s1", "part_class": "battery bay", "issue": "missing", "category": "setup", "severity": "functional",
        "fix": "Put the batteries in, then test again.", "location": ["status": "computed", "point_m": [0.0, 0.1, 0.2]],
        "crop": ["still_id": "s3", "box_px": [10, 20, 110, 220]],
    ] }

    /// Greptile #29: the stage's "Setup to check" rows carry their fix, in the calm setup tone, and the detail says it.
    @Test func setupRowsReadWithTheirFixInTheSetupTone() throws {
        let m = ReportModel(json: ["verdict": "potential_issues", "most_likely": [], "findings": [], "not_inspected": [],
                                   "insufficient_evidence": [], "setup": [Self.battery]])
        let s = try #require(m.setup.first)
        #expect(s.tone == .setup && s.fix == "Put the batteries in, then test again.")
        #expect(ReportWords.setupLabel(s, number: 2) == "2: Battery bay, Looks missing. Put the batteries in, then test again.")
        // a setup state that is a hazard stays a finding, in the danger tone
        let hazard = ReportModel(json: ["verdict": "potential_issues", "most_likely": ["h1"], "not_inspected": [],
                                        "insufficient_evidence": [],
                                        "findings": [["finding_id": "h1", "part_class": "AA cell", "issue": "reversed",
                                                      "category": "setup", "severity": "safety", "fix": "Turn it round."]]])
        #expect(hazard.mostLikely.first?.tone == .danger)
    }

    /// Greptile #29: located setup rows are marked on the stage (3D pins, numbered photos, so "Show on object" has
    /// them), numbered after the problems in the setup tone, and never counted as problems.
    @Test func setupRowsAreMarkedOnTheStageButNeverCountedAsProblems() {
        let m = ReportModel(json: [
            "verdict": "potential_issues", "most_likely": ["f1"], "not_inspected": [], "insufficient_evidence": [],
            "findings": [["finding_id": "f1", "part_class": "fan", "issue": "burnt", "severity": "functional",
                          "location": ["status": "computed", "point_m": [0.1, 0.2, 0.3]]]],
            "setup": [Self.battery],
        ])
        let pins = StageMarks.pins(m)
        #expect(pins.map(\.id) == ["f1", "s1"] && pins.map(\.number) == [1, 2])
        #expect(pins.map(\.tone) == [.caution, .setup] && pins.allSatisfy { $0.ringRadius == nil })
        let marks = StageMarks.photoMarks(m)
        #expect(marks.map(\.id) == ["s1"] && marks.first?.number == 2 && marks.first?.tone == .setup)
        #expect(StageMarks.setupNumber(m, 0) == 2)
        #expect(ReportWords.eyebrow(m) == "1 likely problem")   // the setup row is not a problem
        // a setup-only report still has a marker to show on the object
        let only = ReportModel(json: ["verdict": "no_problems_seen", "most_likely": [], "findings": [], "not_inspected": [],
                                      "insufficient_evidence": [], "setup": [Self.battery]])
        #expect(StageMarks.pins(only).map(\.number) == [1] && StageMarks.pins(only).first?.tone == .setup)
        #expect(ReportWords.eyebrow(only).hasSuffix("1 thing to set up") && !ReportWords.eyebrow(only).contains("problem"))
    }
}

/// Report rebuilds (continued scans): superseded and grouped rows are shown once.
struct ReportRebuildTests {
    /// After a rebuild a newer row can name the older one in `superseded_ids`, and a grouped "could not judge" row lists
    /// its members in `finding_ids`: each thing is shown and counted once.
    @Test func rebuiltReportCountsEachFindingOnce() {
        let json: [String: Any] = [
            "verdict": "potential_issues",
            "findings": [
                ["finding_id": "f1", "part_class": "part", "issue": "damaged"],
                ["finding_id": "f2", "part_class": "part", "issue": "damaged", "superseded_ids": ["f1"]],
                ["finding_id": "f2", "part_class": "part", "issue": "damaged"],
                ["finding_id": "f3", "part_class": "part", "issue": "loose"],
            ],
            "insufficient_evidence": [
                ["finding_id": "g1", "reason": "blurred", "finding_ids": ["g1", "g2", "g3"]],
                ["finding_id": "g2", "reason": "blurred"],
            ],
            "not_inspected": [], "most_likely": ["f2"],
        ]
        let m = ReportModel(json: json)
        #expect(m.mostLikely.map(\.id) == ["f2"])
        #expect(m.otherIssues.map(\.id) == ["f3"])
        #expect(m.insufficient.count == 1)
    }
}

/// The cloud under the pins is the scan's own (segment 0) cloud, never a later unaligned segment's (Greptile, PR #7).
struct ModelSourceTests {
    @Test func theModelIsTheScansOwnCloudNotALaterSegments() throws {
        let dir = FileManager.default.temporaryDirectory.appending(path: "repairos-model-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        for n in 1...6 { try Data("ply\n".utf8).write(to: dir.appending(path: "points_segment\(n)_arkit_world.ply")) }
        try Data("ply\n".utf8).write(to: dir.appending(path: "points_arkit_world.ply"))
        guard case .pointCloud(let url) = ModelSource.find(in: dir) else { Issue.record("no cloud found"); return }
        #expect(url.lastPathComponent == "points_arkit_world.ply")

        try FileManager.default.removeItem(at: dir.appending(path: "points_arkit_world.ply"))
        #expect(ModelSource.find(in: dir).isNone)       // only another frame's clouds: nothing under the pins
    }
}
