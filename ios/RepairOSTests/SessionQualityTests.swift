import Foundation
import Testing
@testable import RepairOS

/// The on-device check of a finished capture: one sentence per failed check, `quality.json` beside the scan package.
struct SessionQualityTests {
    private func still(_ cell: String?, sharp: Double = 100, close: Bool = false, low: Bool = false, reliable: Bool = true)
        -> SessionFacts.StillFact {
        .init(sharpness: sharp, cell: cell, closeUp: close, lowLight: low, poseReliable: reliable)
    }

    /// A scan that walked all the way round: 8 sides, 2 from above, sharp, lit, tracked, a LiDAR cloud.
    private func goodFacts() -> SessionFacts {
        var f = SessionFacts()
        f.scanID = "scan_a"
        f.sessionID = "sess_a"
        f.hasLiDAR = true
        f.depthSource = "lidar"
        f.depthPoints = 20_000
        f.keyframes = 40
        f.scanSeconds = 60
        f.freeBytes = 8_000_000_000
        f.stills = (0..<8).map { still(String(format: "r0s%02d", $0)) } + (0..<2).map { still(String(format: "r2s%02d", $0)) }
        return f
    }

    private func ids(_ r: QualityReport) -> [String] { r.findings.map(\.id) }

    @Test func aWalkRoundTheObjectIsGood() {
        let r = SessionQuality.evaluate(goodFacts())
        #expect(r.verdict == .good && r.findings.isEmpty && r.problems.isEmpty)
        #expect(r.stills.count == 10 && r.coverage.sideSectors == 8 && r.coverage.topSectors == 2)
    }

    @Test func noPhotosAndFewPhotosAreBlockers() {
        var f = goodFacts()
        f.stills = []
        var r = SessionQuality.evaluate(f)
        #expect(r.verdict == .poor && ids(r).first == "no_photos")
        f.stills = [still("r0s00"), still("r0s01"), still("r0s02")]
        r = SessionQuality.evaluate(f)
        #expect(r.findings.first?.id == "few_photos" && r.findings.first?.severity == .blocker)
        #expect(r.findings.first?.message.hasPrefix("Only 3 photos were saved.") == true)
        f.stills = [still("r0s00")]
        #expect(SessionQuality.evaluate(f).findings.first?.message.hasPrefix("Only 1 photo was saved.") == true)
    }

    @Test func missingSidesNameTheFixAndHowFarOff() {
        var f = goodFacts()
        f.stills = (0..<5).map { still(String(format: "r0s%02d", $0)) } + [still("r2s00")]
        var r = SessionQuality.evaluate(f)
        let coverage = r.findings.first { $0.id == "coverage" }
        #expect(coverage?.severity == .warning)
        #expect(coverage?.message == "Photos cover 5 of 8 sides. Walk the rest of the way around it, including the back.")
        f.stills = (0..<3).map { still(String(format: "r0s%02d", $0)) } + [still("r2s00")]
        r = SessionQuality.evaluate(f)
        #expect(r.findings.first { $0.id == "coverage" }?.severity == .blocker && r.verdict == .poor)
    }

    @Test func sidesFromAboveAndBelowAreNotCountedAsSides() {
        let (side, top) = SessionQuality.sectors(["r0s01", "r1s01", "r2s03", "r2s03", "b0s02", "junk", "rXs1"])
        #expect(side == 1 && top == 1)   // r0s01 and r1s01 are the same direction; the underside's b-cells are another scan
    }

    @Test func missingSidesAndMissingTopAreBothReported() {
        var f = goodFacts()
        f.stills = (0..<5).map { still(String(format: "r0s%02d", $0)) }
        let r = SessionQuality.evaluate(f)
        #expect(ids(r).contains("coverage") && ids(r).contains("top"))
    }

    @Test func aReportThatCouldNotBeWrittenSaysSoOnTheReview() throws {
        // A scan folder that does not exist: reading finds nothing and the write fails.
        let root = FileManager.default.temporaryDirectory.appending(path: "lea_quality_nowrite_\(UUID().uuidString)")
        let storage = ScanStorage(root: root)
        let record = try storage.newSession(symptom: nil, engine: .arkit)
        try FileManager.default.removeItem(at: storage.scanDir(record))
        defer { try? FileManager.default.removeItem(at: root) }
        let report = SessionQuality.inspect(storage: storage, record: record, hasLiDAR: false, objectLocked: false,
                                            objectSizeAssumed: false, scanSeconds: nil, freeBytes: nil, closeUpSides: nil)
        #expect(report.findings.contains { $0.id == "not_saved" })
        #expect(!SessionQuality.recordDecision(uploadedAnyway: true, scanDir: storage.scanDir(record)))
    }

    @Test func noPhotoFromAboveIsAskedFor() {
        var f = goodFacts()
        f.stills = (0..<10).map { still(String(format: "r%ds%02d", $0 % 2, $0)) }
        #expect(ids(SessionQuality.evaluate(f)).contains("top"))
    }

    @Test func blurryPhotosAreCountedAgainstTheSetsOwnMedian() {
        var f = goodFacts()
        f.stills = (0..<10).map { still(String(format: "r%ds%02d", 0, $0 % 8), sharp: $0 < 6 ? 100 : 20) } + [still("r2s00"), still("r2s01")]
        // 4 of 12 below 0.4 x the median: a third, under the line.
        #expect(!ids(SessionQuality.evaluate(f)).contains("blurry"))
        f.stills = (0..<10).map { still(String(format: "r0s%02d", $0 % 8), sharp: $0 < 5 ? 100 : 20) } + [still("r2s00"), still("r2s01")]
        let r = SessionQuality.evaluate(f)
        #expect(r.findings.first { $0.id == "blurry" }?.message.hasPrefix("5 of 12 photos are blurry.") == true)
        #expect(r.stills.blurry == 5 && r.verdict == .fair)
    }

    @Test func dimLightAndLostTrackingAreNamed() {
        var f = goodFacts()
        f.stills = f.stills.map { still($0.cell, low: true, reliable: false) }
        let r = SessionQuality.evaluate(f)
        #expect(ids(r).contains("dim") && ids(r).contains("tracking"))
        #expect(r.stills.lowLight == 10 && r.stills.poseUnreliable == 10)
    }

    @Test func aPhoneWithoutLiDARGetsANoteNotAProblem() {
        var f = goodFacts()
        f.hasLiDAR = false
        f.depthSource = "none"
        f.depthPoints = 0
        let r = SessionQuality.evaluate(f)
        #expect(r.verdict == .good && r.problems.isEmpty)
        #expect(r.notes.map(\.id) == ["no_depth"])
        #expect(r.notes.first?.message.contains("photos alone") == true)
        // A LiDAR phone with a thin cloud is a warning.
        f.hasLiDAR = true
        f.depthSource = "lidar"
        f.depthPoints = 300
        #expect(ids(SessionQuality.evaluate(f)).contains("thin_depth"))
    }

    @Test func anEstimatedBoxSizeAndAnUnlockedObjectAreSaid() {
        var f = goodFacts()
        f.objectSizeAssumed = true
        #expect(SessionQuality.evaluate(f).notes.map(\.id) == ["size_assumed"])
        f.objectLocked = false
        let r = SessionQuality.evaluate(f)
        #expect(ids(r).contains("not_locked") && !ids(r).contains("coverage"))
    }

    @Test func aVeryQuickScanIsNamedOnlyWhenTheViewsAreShort() {
        var f = goodFacts()
        f.scanSeconds = 3
        #expect(!ids(SessionQuality.evaluate(f)).contains("quick"))   // 8 sides in 3 s: nothing to say
        f.stills = [still("r0s00"), still("r0s01"), still("r0s02"), still("r0s03"), still("r0s04")]
        #expect(ids(SessionQuality.evaluate(f)).contains("quick"))
    }

    @Test func lowStorageIsAWarningAndAlmostNoneABlocker() {
        var f = goodFacts()
        f.freeBytes = 300_000_000
        var r = SessionQuality.evaluate(f)
        #expect(r.findings.first?.id == "storage" && r.findings.first?.severity == .warning)
        f.freeBytes = 60_000_000
        r = SessionQuality.evaluate(f)
        #expect(r.findings.first?.severity == .blocker && r.verdict == .poor)
    }

    @Test func closeUpsThatAreShortAreAskedFor() {
        var f = goodFacts()
        f.closeUpSidesDone = 2
        f.closeUpSidesRequired = 5
        #expect(SessionQuality.evaluate(f).findings.first { $0.id == "close_ups" }?.message
                == "Close-ups cover 2 of 5 sides. Move in close to the sides that are still missing.")
        f.closeUpSidesDone = 5
        #expect(!ids(SessionQuality.evaluate(f)).contains("close_ups"))
    }

    @Test func blockersComeFirstThenWarningsThenNotes() {
        var f = goodFacts()
        f.hasLiDAR = false
        f.freeBytes = 300_000_000
        f.stills = [still("r0s00"), still("r0s01")]
        let severities = SessionQuality.evaluate(f).findings.map(\.severity)
        #expect(severities == severities.sorted { rank($0) < rank($1) })
        #expect(severities.first == .blocker && severities.last == .note)
    }

    private func rank(_ s: QualityFinding.Severity) -> Int {
        switch s {
        case .blocker: 0
        case .warning: 1
        case .note: 2
        }
    }

    // MARK: quality.json

    @Test func theReportKeepsItsFileFieldNames() throws {
        let data = try JSON.encoder.encode(SessionQuality.evaluate(goodFacts()))
        let json = try #require(JSONSerialization.jsonObject(with: data) as? [String: Any])
        for key in ["schema_version", "scan_id", "session_id", "checked_at", "verdict", "stills", "coverage", "depth",
                    "object_locked", "object_size_assumed", "scan_seconds", "free_bytes", "findings"] {
            #expect(json[key] != nil, "missing \(key)")
        }
        #expect(json["uploaded_anyway"] == nil)   // absent until the person chooses
        let stills = try #require(json["stills"] as? [String: Any])
        #expect(Set(stills.keys) == ["count", "close_ups", "blurry", "low_light", "pose_unreliable"])
        let coverage = try #require(json["coverage"] as? [String: Any])
        #expect(coverage["side_sectors"] as? Int == 8 && coverage["top_sectors"] as? Int == 2 && coverage["side_target"] as? Int == 8)
        #expect(json["schema_version"] as? Int == 1 && json["verdict"] as? String == "good")
    }

    @Test func theFolderIsReadJudgedAndWrittenBesideTheScanPackage() throws {
        let root = FileManager.default.temporaryDirectory.appending(path: "lea_quality_\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: root) }
        let storage = ScanStorage(root: root)
        var record = try storage.newSession(symptom: nil, engine: .arkit)
        // 6 photos on disk: four cells, one of them a close-up, one without its image (not counted).
        var ids: [String] = []
        for (i, cell) in ["r0s00", "r0s01", "r0s02", "r0s03", "r2s00", "r0s04"].enumerated() {
            let id = "\(record.scanID)_s\(i + 1)"
            ids.append(id)
            let json: [String: Any] = ["kind": i == 1 ? "guided" : "unguided", "view_cell": cell, "sharpness": 90.0,
                                       "low_light": false, "pose_reliable": true]
            try JSONSerialization.data(withJSONObject: json).write(to: storage.stillURL(record, id))
            if i != 5 { try Data([0xFF, 0xD8]).write(to: storage.stillsDir(record).appending(path: "\(id).jpg")) }
        }
        record.stillIDs = ids
        let package: [String: Any] = ["depth_source": "lidar", "point_count": 12_345, "keyframes": [1, 2, 3]]
        try JSONSerialization.data(withJSONObject: package).write(to: storage.scanPackageURL(record))

        let report = SessionQuality.inspect(storage: storage, record: record, hasLiDAR: true, objectLocked: true,
                                            objectSizeAssumed: false, scanSeconds: 42, freeBytes: 5_000_000_000, closeUpSides: nil)
        #expect(report.stills.count == 5 && report.stills.closeUps == 1)
        #expect(report.coverage.sideSectors == 4 && report.coverage.topSectors == 1)
        #expect(report.depth.points == 12_345 && report.depth.keyframes == 3 && report.depth.source == "lidar")
        #expect(report.findings.contains { $0.id == "coverage" })

        let file = storage.scanDir(record).appending(path: "quality.json")
        #expect(FileManager.default.fileExists(atPath: file.path))
        #expect(file.deletingLastPathComponent() == storage.scanPackageURL(record).deletingLastPathComponent())
        let again = try #require(SessionQuality.read(scanDir: storage.scanDir(record)))
        #expect(again == report && again.uploadedAnyway == nil)

        // The person's choice is recorded on the same file.
        SessionQuality.recordDecision(uploadedAnyway: true, scanDir: storage.scanDir(record))
        #expect(SessionQuality.read(scanDir: storage.scanDir(record))?.uploadedAnyway == true)
        SessionQuality.recordDecision(uploadedAnyway: false, scanDir: storage.scanDir(record))
        #expect(SessionQuality.read(scanDir: storage.scanDir(record))?.uploadedAnyway == false)
    }

    @Test func aFolderWithNothingInItIsAPoorScanNotACrash() throws {
        let root = FileManager.default.temporaryDirectory.appending(path: "lea_quality_empty_\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: root) }
        let storage = ScanStorage(root: root)
        let record = try storage.newSession(symptom: nil, engine: .arkit)
        let report = SessionQuality.inspect(storage: storage, record: record, hasLiDAR: false, objectLocked: false,
                                            objectSizeAssumed: false, scanSeconds: nil, freeBytes: nil, closeUpSides: nil)
        #expect(report.verdict == .poor && report.stills.count == 0)
        #expect(report.findings.first?.id == "no_photos")
        // Nothing on disk is read as a made-up number.
        #expect(report.depth.points == 0 && report.depth.source == "none")
    }

    @Test func theReviewSummaryReadsInPlainWords() {
        var r = SessionQuality.evaluate(goodFacts())
        #expect(CaptureReviewView.summary(r) == "10 photos, 8 of 8 sides.")
        r.stills.closeUps = 1
        r.stills.count = 1
        #expect(CaptureReviewView.summary(r) == "1 photo, 1 close-up, 8 of 8 sides.")
    }
}
