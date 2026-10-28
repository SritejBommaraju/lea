import ARKit
import Foundation
import simd

// Capture guidance as pure functions: what is said to a first-time customer, in what order, and when it is kept
// quiet. Nothing here touches ARKit state or the camera, so the rules are unit-tested (CaptureGuidanceTests).
// One cue on screen at a time: an alert (the capture itself is in trouble) beats a warning, a warning beats a lead,
// a lead beats the steady "keep walking" line.

// MARK: - Alerts

/// Something about the capture itself (not the framing): the camera was taken away, the phone lost its place, the
/// phone is out of room or too hot. An alert replaces every other cue while it is up.
enum CaptureAlert: Equatable, Sendable {
    /// A call, Siri, another app or the lock screen took the camera.
    case interrupted
    /// ARKit lost the phone's position (it is finding it again).
    case trackingLost
    case storageFull
    /// The phone's thermal state is critical.
    case overheating
    /// Serious: the light is dimmed, scanning carries on.
    case hot
    case lowPower

    /// What kind of trouble it is: one alert per kind at a time.
    enum Kind: CaseIterable, Sendable { case camera, tracking, storage, thermal, power }

    var kind: Kind {
        switch self {
        case .interrupted: .camera
        case .trackingLost: .tracking
        case .storageFull: .storage
        case .overheating, .hot: .thermal
        case .lowPower: .power
        }
    }

    /// Higher wins when several are up.
    var priority: Int {
        switch self {
        case .storageFull: 60
        case .overheating: 50
        case .interrupted: 40
        case .trackingLost: 30
        case .hot: 20
        case .lowPower: 10
        }
    }

    /// Photos and depth keyframes stop while this is up. The data already taken is kept.
    var pausesShooting: Bool {
        switch self {
        case .interrupted, .trackingLost, .storageFull, .overheating: true
        case .hot, .lowPower: false
        }
    }

    /// The words, one sentence that also says what to do and what happened to the data.
    var text: String {
        switch self {
        case .interrupted: "Paused: another app has the camera. Your photos are kept"
        case .trackingLost: "Lost its place: go back to where you were"
        case .storageFull: "Almost out of storage: tap Done to keep what you have"
        case .overheating: "The phone is too hot: tap Done to keep what you have"
        case .hot: "The phone is warm: the light is dimmed"
        case .lowPower: "Low Power Mode: scanning may be slower"
        }
    }

    var symbol: String {
        switch self {
        case .interrupted: "pause.circle.fill"
        case .trackingLost: "location.slash.fill"
        case .storageFull: "externaldrive.fill.badge.exclamationmark"
        case .overheating, .hot: "thermometer.high"
        case .lowPower: "battery.25"
        }
    }

    var tone: CueBanner.Tone {
        switch self {
        case .interrupted, .trackingLost, .storageFull, .overheating: .stop
        case .hot, .lowPower: .caution
        }
    }

    /// Informational alerts go away by themselves, so a phone that stays warm does not hide the guidance for the
    /// whole scan.
    var timeToLive: TimeInterval? {
        switch self {
        case .hot: 8
        case .lowPower: 6
        default: nil
        }
    }
}

/// The alerts that are up, one per kind.
struct AlertBoard: Sendable {
    private var active: [CaptureAlert.Kind: (alert: CaptureAlert, since: TimeInterval)] = [:]

    /// Sets (or, with nil, clears) the alert of one kind. Raising the alert that is already up keeps its start time.
    mutating func set(_ kind: CaptureAlert.Kind, to alert: CaptureAlert?, now: TimeInterval) {
        guard let alert else {
            active[kind] = nil
            return
        }
        if active[kind]?.alert == alert { return }
        active[kind] = (alert, now)
    }

    /// The alert to show: the highest priority one that has not timed out.
    func current(now: TimeInterval) -> CaptureAlert? {
        active.values
            .filter { entry in entry.alert.timeToLive.map { now - entry.since < $0 } ?? true }
            .map { entry in entry.alert }
            .max { $0.priority < $1.priority }
    }

    /// Shooting stops (there is no timeout for these).
    func pausesShooting(now: TimeInterval) -> Bool {
        active.values.contains { entry in
            entry.alert.pausesShooting && (entry.alert.timeToLive.map { now - entry.since < $0 } ?? true)
        }
    }
}

/// ARKit's tracking state as an alert. Weak tracking from motion or a bare surface is not one (the cue says "move
/// slowly" and the photo is kept with `pose_reliable` false); only a lost position is, because the poses of what is
/// taken then would not agree with the earlier ones.
enum TrackingAdvice {
    static func alert(_ state: ARCamera.TrackingState, hadNormal: Bool) -> CaptureAlert? {
        switch state {
        case .normal:
            return nil
        case .notAvailable:
            return hadNormal ? .trackingLost : nil
        case .limited(let reason):
            if case .relocalizing = reason { return .trackingLost }
            return nil
        }
    }
}

/// Phone health as alerts (pure: the caller reads `ProcessInfo`).
enum DeviceHealth {
    static func thermal(_ state: ProcessInfo.ThermalState) -> CaptureAlert? {
        switch state {
        case .critical: .overheating
        case .serious: .hot
        default: nil
        }
    }

    /// Below this much free space the scan stops taking photos: a 12 MP still is about 3 MB and the session also
    /// writes depth, a world map and a point cloud.
    static let storageFloorBytes: Int64 = 150_000_000

    static func storage(freeBytes: Int64?) -> CaptureAlert? {
        guard let freeBytes, freeBytes < storageFloorBytes else { return nil }
        return .storageFull
    }

    static func power(lowPowerMode: Bool) -> CaptureAlert? { lowPowerMode ? .lowPower : nil }

    /// Free space for important usage (what iOS allows an app to use), nil when unknown.
    static func freeBytes(at url: URL = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]) -> Int64? {
        let values = try? url.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey, .volumeAvailableCapacityKey])
        if let important = values?.volumeAvailableCapacityForImportantUsage, important > 0 { return important }
        return values?.volumeAvailableCapacity.map { Int64($0) }
    }
}

// MARK: - One cue at a time

enum CueArbiter {
    enum Winner: Equatable, Sendable { case alert, cue, lead }

    /// - Parameters:
    ///   - alert: the alert that is up, if any.
    ///   - cueTone: the tone of the session's own cue (nil = it has none).
    ///   - leadShowing: a researched lead ("look closely here") is up.
    /// An alert always wins. A lead (guidance about what the person suspects) gives way to a warning (`caution` or
    /// `stop`: blur, light, distance), because a warning is about the photo being taken now.
    static func winner(alert: CaptureAlert?, cueTone: CueBanner.Tone?, leadShowing: Bool) -> Winner {
        if alert != nil { return .alert }
        if leadShowing, cueTone != .caution, cueTone != .stop { return .lead }
        return .cue
    }
}

/// Keeps a warning off the screen until it has lasted, and on it long enough to read. A shaky frame or two must not
/// flash "Blurry: hold still" at someone who is walking normally; a warning that does stay is shown for at least
/// `minHold` after the cause is gone. Guidance and "all good" cues pass straight through.
struct CueDebouncer: Sendable {
    static let cautionDelay: TimeInterval = 0.8
    static let minHold: TimeInterval = 1.2

    private(set) var shown: String?
    private var shownTone: CueBanner.Tone = .guide
    private var shownSince: TimeInterval = 0
    private var pending: String?
    private var pendingSince: TimeInterval = 0

    /// Feeds the cue the session wants on screen now; returns what to show.
    mutating func update(_ text: String?, tone: CueBanner.Tone, now: TimeInterval) -> String? {
        if text == shown {
            pending = text
            pendingSince = now
            return shown
        }
        if text != pending {
            pending = text
            pendingSince = now
        }
        let delay: TimeInterval = (text != nil && tone == .caution) ? Self.cautionDelay : 0
        let heldLongEnough = now - pendingSince >= delay
        // A stop cue (too close, back up) always shows at once; only quieter cues wait for the warning's minimum hold.
        let leavingWarning = tone != .stop && shown != nil && shownTone == .caution && now - shownSince < Self.minHold
        if heldLongEnough, !leavingWarning {
            shown = text
            shownTone = tone
            shownSince = now
        }
        return shown
    }
}

// MARK: - Where to go next, in words

extension CoverageDome {
    /// One sentence for the way to the nearest side still missing a photo: which way round (the short way), and up
    /// or down. Moving the camera to the person's right raises the azimuth (the dome's sectors run that way), so
    /// "right" is the direction of increasing sector.
    static func walkCue(fromSector here: Int, ring hereRing: Int, to cell: Int) -> String {
        let target = cell % sectors, targetRing = cell / sectors
        let delta = ((target - here) % sectors + sectors) % sectors
        let up = targetRing > hereRing, down = targetRing < hereRing
        let vertical = up ? "go higher" : (down ? "go lower" : nil)
        let horizontal: String? = switch delta {
        case 0: nil
        case 1, 2: "walk a little to the right"
        case 3, 4, 5: "walk right around it"
        case 6: "go around to the other side"
        case 7, 8, 9: "walk left around it"
        default: "walk a little to the left"
        }
        switch (horizontal, vertical) {
        case let (h?, v?): return "\(h.prefix(1).uppercased() + h.dropFirst()) and \(v)"
        case let (h?, nil): return h.prefix(1).uppercased() + h.dropFirst()
        case let (nil, v?): return v.prefix(1).uppercased() + v.dropFirst()
        default: return "Keep going"
        }
    }

}

/// The plain-words coverage line under the dial: how many of the sides are seen, and the close-ups.
enum CoverageSummary {
    /// "5 of 8 sides"; the target is `ScanProgress.sideSectors`.
    static func sides(_ dome: CoverageDome) -> String {
        "\(min(dome.sideSectors, ScanProgress.sideSectors)) of \(ScanProgress.sideSectors) sides"
    }

    /// "2 of 5 close-ups", or nil when the object needs none.
    static func closeUps(_ dome: CoverageDome) -> String? {
        dome.requiredFaces > 0 ? "\(min(dome.closeFaces.count, dome.requiredFaces)) of \(dome.requiredFaces) close-ups" : nil
    }
}

// MARK: - Close-ups in plain words

/// Which side of the object a close-up is wanted of, as the person sees it right now.
enum FaceWords {
    /// "the top", "the side facing you", "the far side", "the left side", "the right side".
    static func name(face: Int, box: ObjectBox, camera: SIMD3<Float>) -> String {
        if face == 0 { return "the top" }
        let rotation = simd_quatf(angle: box.yaw, axis: SIMD3(0, 1, 0))
        let n = rotation.act(CoverageDome.faceNormal(face))
        var toCamera = SIMD3(camera.x - box.center.x, 0, camera.z - box.center.z)
        guard simd_length(toCamera) > 1e-4 else { return "the side facing you" }
        toCamera = simd_normalize(toCamera)
        // The camera looks along -toCamera; its right-hand side is (-f.z, 0, f.x).
        let look = -toCamera
        let right = SIMD3(-look.z, 0, look.x)
        let facing = simd_dot(n, toCamera)
        if facing > 0.7 { return "the side facing you" }
        if facing < -0.7 { return "the far side" }
        return simd_dot(n, right) > 0 ? "the right side" : "the left side"
    }
}

/// The close-up prompt: what to photograph and how far along the close-ups are.
struct CloseupPrompt: Equatable, Sendable {
    var words: String
    /// The one being asked for (1-based) and how many are needed.
    var index: Int
    var total: Int

    var sentence: String { "Move closer to \(words) (\(index) of \(total))" }

    static func make(face: Int, box: ObjectBox, camera: SIMD3<Float>, dome: CoverageDome) -> CloseupPrompt? {
        guard dome.requiredFaces > 0 else { return nil }
        return CloseupPrompt(words: FaceWords.name(face: face, box: box, camera: camera),
                             index: min(dome.requiredFaces, dome.closeFaces.count + 1), total: dome.requiredFaces)
    }
}

// MARK: - A capture that was cut off

/// What an earlier run of the same scan left in its folder (the app was closed or killed mid-capture). A new run
/// numbers its photos and keyframes after these so it never overwrites them.
enum ExistingCapture {
    struct Found: Equatable, Sendable {
        var highestStill: Int
        var keyframeCount: Int
        var isEmpty: Bool { highestStill == 0 && keyframeCount == 0 }
    }

    static func find(stillsDir: URL, scanDir: URL, scanID: String) -> Found {
        let fm = FileManager.default
        let stills = ((try? fm.contentsOfDirectory(atPath: stillsDir.path)) ?? []).compactMap { name -> Int? in
            guard name.hasPrefix("\(scanID)_s") else { return nil }
            let stem = name.split(separator: ".").first.map(String.init) ?? name
            return stem.split(separator: "_").last.flatMap { $0.dropFirst().isEmpty ? nil : Int($0.dropFirst()) }
        }
        let keyframes = ((try? fm.contentsOfDirectory(atPath: scanDir.path)) ?? []).compactMap { name -> Int? in
            guard name.hasPrefix("kf_"), name.hasSuffix(".jpg"), !name.contains("_depth"), !name.contains("_conf") else { return nil }
            return Int(name.dropFirst(3).prefix(4))
        }
        return Found(highestStill: stills.max() ?? 0, keyframeCount: (keyframes.max() ?? -1) + 1)
    }
}
