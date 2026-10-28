import Foundation

// The on-device check of a finished capture, before the report is asked for. It reads what is in the session folder
// (the photos' own JSON and the scan package), so it judges what will really be sent. One sentence per failed check,
// each with the fix. The result is written beside the scan package as `scans/<scan_id>/quality.json`; it is not part
// of the scan package contract, the gateway reads it when it wants to.
//
// The thresholds are first values chosen from how the capture engine works (8 sides is what its progress needs; a
// photo set that is mostly soft against its own median is blurry), not calibrated on recorded captures yet. They are
// named constants so a recorded session can move them.

struct QualityFinding: Codable, Equatable, Sendable {
    enum Severity: String, Codable, Sendable {
        /// The report would be unreliable: say so before it is asked for.
        case blocker
        /// Worth fixing; the report can still be made.
        case warning
        /// Information the report will state anyway.
        case note
    }

    var id: String
    var severity: Severity
    /// One sentence: what is wrong and what to do.
    var message: String
}

struct QualityReport: Codable, Equatable, Sendable {
    enum Verdict: String, Codable, Sendable { case good, fair, poor }

    struct Stills: Codable, Equatable, Sendable {
        var count: Int
        var closeUps: Int
        var blurry: Int
        var lowLight: Int
        var poseUnreliable: Int

        enum CodingKeys: String, CodingKey {
            case count, blurry
            case closeUps = "close_ups"
            case lowLight = "low_light"
            case poseUnreliable = "pose_unreliable"
        }
    }

    struct Coverage: Codable, Equatable, Sendable {
        /// Distinct directions (of 12 around the object) with a photo from the side, and from above.
        var sideSectors: Int
        var topSectors: Int
        /// The sides the scan wants (`ScanProgress.sideSectors`) and the close-up sides it asked for.
        var sideTarget: Int
        var closeUpSidesDone: Int?
        var closeUpSidesRequired: Int?

        enum CodingKeys: String, CodingKey {
            case sideSectors = "side_sectors"
            case topSectors = "top_sectors"
            case sideTarget = "side_target"
            case closeUpSidesDone = "close_up_sides_done"
            case closeUpSidesRequired = "close_up_sides_required"
        }
    }

    struct Depth: Codable, Equatable, Sendable {
        /// "lidar" or "none" (a phone without LiDAR: the report works from the photos).
        var source: String
        var points: Int
        var keyframes: Int
    }

    var schemaVersion = 1
    var scanID: String
    var sessionID: String
    var checkedAt: Date
    var verdict: Verdict
    var stills: Stills
    var coverage: Coverage
    var depth: Depth
    var objectLocked: Bool
    /// The object's box size was a default (no depth and too few feature points), not measured.
    var objectSizeAssumed: Bool
    var scanSeconds: Double?
    var freeBytes: Int64?
    var findings: [QualityFinding]
    /// Set once the person chose: true = continued despite problems, false = went back to scan more. Absent until then.
    var uploadedAnyway: Bool?

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case scanID = "scan_id"
        case sessionID = "session_id"
        case checkedAt = "checked_at"
        case verdict, stills, coverage, depth, findings
        case objectLocked = "object_locked"
        case objectSizeAssumed = "object_size_assumed"
        case scanSeconds = "scan_seconds"
        case freeBytes = "free_bytes"
        case uploadedAnyway = "uploaded_anyway"
    }

    /// Findings that ask the person to do something (blockers first), without the notes.
    var problems: [QualityFinding] { findings.filter { $0.severity != .note } }
    var notes: [QualityFinding] { findings.filter { $0.severity == .note } }
}

/// What the checks look at, gathered from the folder (or built by hand in a test).
struct SessionFacts: Sendable, Equatable {
    struct StillFact: Sendable, Equatable {
        var sharpness: Double?
        /// "r1s05" (ring 1, sector 5), nil when the photo has no cell.
        var cell: String?
        var closeUp: Bool
        var lowLight: Bool
        var poseReliable: Bool
    }

    var scanID = ""
    var sessionID = ""
    var stills: [StillFact] = []
    var hasLiDAR = false
    var depthSource = "none"
    var depthPoints = 0
    var keyframes = 0
    var objectLocked = true
    var objectSizeAssumed = false
    var scanSeconds: Double?
    var freeBytes: Int64?
    var closeUpSidesDone: Int?
    var closeUpSidesRequired: Int?
}

enum SessionQuality {
    /// Fewer saved photos than this and the report has too little to go on.
    static let minStills = 4
    /// Below this many of the wanted sides a photo set is hopeless; between this and the target it is partial.
    static let minSides = 4
    /// A photo softer than this share of the set's median sharpness is blurry; this share of blurry ones is a problem.
    static let blurryRatio = 0.4
    static let blurryShare = 0.35
    static let lowLightShare = 0.5
    static let poseUnreliableShare = 0.3
    /// A LiDAR phone with fewer in-box points than this has barely seen the shape.
    static let minDepthPoints = 2_000
    static let minScanSeconds = 6.0
    static let storageBlockerBytes: Int64 = 150_000_000
    static let storageWarningBytes: Int64 = 500_000_000
    /// The check needs at least this many photos before it compares sharpness to the median.
    static let minForSharpness = 4

    // MARK: Pure evaluation

    static func evaluate(_ f: SessionFacts, now: Date = Date()) -> QualityReport {
        var findings: [QualityFinding] = []
        func add(_ id: String, _ severity: QualityFinding.Severity, _ message: String) {
            findings.append(QualityFinding(id: id, severity: severity, message: message))
        }

        let n = f.stills.count
        let (side, top) = sectors(f.stills.compactMap(\.cell))
        let sideTarget = ScanProgress.sideSectors

        if n == 0 {
            add("no_photos", .blocker, "No photos were saved. Tap Scan, then walk slowly around the object.")
        } else if n < minStills {
            add("few_photos", .blocker, "Only \(n) photo\(n == 1 ? " was" : "s were") saved. Walk slowly around the object so it can take more.")
        }

        if n > 0 {
            if !f.objectLocked {
                add("not_locked", .warning, "The object was never locked, so the sides can't be checked. Tap the object on the screen, then scan again.")
            } else if side < sideTarget {
                let msg = "Photos cover \(side) of \(sideTarget) sides. Walk the rest of the way around it, including the back."
                add("coverage", side < minSides ? .blocker : .warning, msg)
            }
            if f.objectLocked, top == 0 {
                add("top", .warning, "Nothing was photographed from above. Hold the phone over it and take a few from the top.")
            }
            if let done = f.closeUpSidesDone, let required = f.closeUpSidesRequired, required > 0, done < required {
                add("close_ups", .warning, "Close-ups cover \(done) of \(required) sides. Move in close to the sides that are still missing.")
            }
        }

        let sharp = f.stills.compactMap(\.sharpness)
        var blurry = 0
        if sharp.count >= minForSharpness {
            let median = sharp.sorted()[sharp.count / 2]
            blurry = sharp.filter { $0 < median * blurryRatio }.count
            if Double(blurry) / Double(sharp.count) > blurryShare {
                add("blurry", .warning, "\(blurry) of \(sharp.count) photos are blurry. Hold still a moment at each spot, and add light if it is dim.")
            }
        }
        let lowLight = f.stills.filter(\.lowLight).count
        if n >= minForSharpness, Double(lowLight) / Double(n) > lowLightShare {
            add("dim", .warning, "Most photos were taken in dim light. Turn on a lamp or move next to a window.")
        }
        let unreliable = f.stills.filter { !$0.poseReliable }.count
        if n >= minForSharpness, Double(unreliable) / Double(n) > poseUnreliableShare {
            add("tracking", .warning, "The phone lost track of its position during the scan. Move more slowly and keep the object in view.")
        }

        if f.hasLiDAR {
            if f.depthPoints < minDepthPoints {
                add("thin_depth", .warning, "The depth scan is thin. Walk around the object slowly so the phone can build its shape.")
            }
        } else {
            add("no_depth", .note, "This phone has no LiDAR, so the report works from the photos alone and can't state sizes.")
        }
        if f.objectLocked, f.objectSizeAssumed {
            add("size_assumed", .note, "The object's size was estimated, not measured.")
        }

        if let s = f.scanSeconds, s < minScanSeconds, n < minStills + 4 || side < sideTarget {
            add("quick", .warning, "That was very quick (\(Int(s)) s). Walk slowly all the way around; sharp photos take time.")
        }

        if let free = f.freeBytes {
            if free < storageBlockerBytes {
                add("storage", .blocker, "The phone is almost out of storage. Free some space, then send the scan.")
            } else if free < storageWarningBytes {
                add("storage", .warning, "The phone is low on storage. Free some space so the upload and the saved report fit.")
            }
        }

        let order: [QualityFinding.Severity: Int] = [.blocker: 0, .warning: 1, .note: 2]
        findings = findings.enumerated().sorted {
            let a = order[$0.element.severity] ?? 3, b = order[$1.element.severity] ?? 3
            return a == b ? $0.offset < $1.offset : a < b
        }.map(\.element)

        let verdict: QualityReport.Verdict = findings.contains { $0.severity == .blocker } ? .poor
            : (findings.contains { $0.severity == .warning } ? .fair : .good)
        return QualityReport(
            scanID: f.scanID, sessionID: f.sessionID, checkedAt: Date(timeIntervalSince1970: now.timeIntervalSince1970.rounded(.down)),
            verdict: verdict,
            stills: .init(count: n, closeUps: f.stills.filter(\.closeUp).count, blurry: blurry, lowLight: lowLight,
                          poseUnreliable: unreliable),
            coverage: .init(sideSectors: side, topSectors: top, sideTarget: sideTarget,
                            closeUpSidesDone: f.closeUpSidesDone, closeUpSidesRequired: f.closeUpSidesRequired),
            depth: .init(source: f.depthSource, points: f.depthPoints, keyframes: f.keyframes),
            objectLocked: f.objectLocked, objectSizeAssumed: f.objectSizeAssumed, scanSeconds: f.scanSeconds.map { ($0 * 10).rounded() / 10 },
            freeBytes: f.freeBytes, findings: findings, uploadedAnyway: nil)
    }

    /// Distinct side and top sectors from cell ids ("r<ring>s<sector>"; the bottom segment's "b..." cells are not these).
    static func sectors(_ cells: [String]) -> (side: Int, top: Int) {
        var side = Set<Int>(), top = Set<Int>()
        for c in cells {
            guard c.hasPrefix("r"), let sIndex = c.firstIndex(of: "s"),
                  let ring = Int(c[c.index(after: c.startIndex)..<sIndex]),
                  let sector = Int(c[c.index(after: sIndex)...]) else { continue }
            if ring >= CoverageDome.rings - 1 { top.insert(sector) } else { side.insert(sector) }
        }
        return (side.count, top.count)
    }

    // MARK: Reading the folder

    /// The facts of a captured session, from its files.
    static func facts(storage: ScanStorage, record: SessionRecord, hasLiDAR: Bool, objectLocked: Bool, objectSizeAssumed: Bool,
                      scanSeconds: Double?, freeBytes: Int64?, closeUpSides: (done: Int, required: Int)?) -> SessionFacts {
        var f = SessionFacts()
        f.scanID = record.scanID
        f.sessionID = record.id
        f.hasLiDAR = hasLiDAR
        f.objectLocked = objectLocked
        f.objectSizeAssumed = objectSizeAssumed
        f.scanSeconds = scanSeconds
        f.freeBytes = freeBytes
        f.closeUpSidesDone = closeUpSides?.done
        f.closeUpSidesRequired = closeUpSides?.required
        let fm = FileManager.default
        for id in record.stillIDs {
            // A photo counts when both its record and its image are on disk.
            guard fm.fileExists(atPath: storage.stillsDir(record).appending(path: "\(id).jpg").path),
                  let data = try? Data(contentsOf: storage.stillURL(record, id)),
                  let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { continue }
            let kind = json["kind"] as? String
            f.stills.append(.init(sharpness: (json["sharpness"] as? NSNumber)?.doubleValue,
                                  cell: json["view_cell"] as? String,
                                  closeUp: kind == "guided" || kind == "macro_detail",
                                  lowLight: (json["low_light"] as? Bool) ?? false,
                                  poseReliable: (json["pose_reliable"] as? Bool) ?? true))
        }
        if let data = try? Data(contentsOf: storage.scanPackageURL(record)),
           let pkg = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] {
            f.depthSource = (pkg["depth_source"] as? String) ?? "none"
            f.depthPoints = (pkg["point_count"] as? NSNumber)?.intValue ?? 0
            f.keyframes = (pkg["keyframes"] as? [Any])?.count ?? 0
        }
        return f
    }

    static func url(in scanDir: URL) -> URL { scanDir.appending(path: "quality.json") }

    @discardableResult
    static func write(_ report: QualityReport, scanDir: URL) -> Bool {
        guard let data = try? JSON.encoder.encode(report) else { return false }
        do {
            try data.write(to: url(in: scanDir), options: .atomic)
            return true
        } catch {
            return false
        }
    }

    static func read(scanDir: URL) -> QualityReport? {
        (try? Data(contentsOf: url(in: scanDir))).flatMap { try? JSON.decoder.decode(QualityReport.self, from: $0) }
    }

    /// Records what the person chose after seeing the findings (so the gateway can tell a reviewed scan from a
    /// hurried one). False when there is no report or it could not be written.
    @discardableResult
    static func recordDecision(uploadedAnyway: Bool, scanDir: URL) -> Bool {
        guard var report = read(scanDir: scanDir) else { return false }
        report.uploadedAnyway = uploadedAnyway
        return write(report, scanDir: scanDir)
    }

    /// Said on the review when `quality.json` could not be written (the phone is out of room): the check still
    /// shows, but the person knows the gateway will not have it.
    static let notSavedNote = QualityFinding(id: "not_saved", severity: .note,
                                             message: "This check could not be saved on the phone, so it will not be sent with the scan.")

    /// Reads the folder, judges it, writes `quality.json`. Off the main thread: it reads a few dozen small files.
    static func inspect(storage: ScanStorage, record: SessionRecord, hasLiDAR: Bool, objectLocked: Bool, objectSizeAssumed: Bool,
                        scanSeconds: Double?, freeBytes: Int64?, closeUpSides: (done: Int, required: Int)?) -> QualityReport {
        let gathered = Self.facts(storage: storage, record: record, hasLiDAR: hasLiDAR, objectLocked: objectLocked,
                                  objectSizeAssumed: objectSizeAssumed, scanSeconds: scanSeconds, freeBytes: freeBytes,
                                  closeUpSides: closeUpSides)
        var report = evaluate(gathered)
        if !write(report, scanDir: storage.scanDir(record)) {
            report.findings.append(notSavedNote)
        }
        return report
    }
}
