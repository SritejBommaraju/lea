import ARKit
import Foundation
import Testing
import simd
@testable import RepairOS

/// What a first-time customer is told while scanning, and in what order: alerts, one cue at a time, quiet warnings,
/// the words for where to walk next, the no-LiDAR box and progress, and a capture that was cut off.
struct CaptureGuidanceTests {
    // MARK: Alerts

    @Test func theHighestPriorityAlertShowsAndInformationalOnesExpire() {
        var board = AlertBoard()
        board.set(.power, to: .lowPower, now: 0)
        #expect(board.current(now: 1) == .lowPower)
        board.set(.tracking, to: .trackingLost, now: 2)
        #expect(board.current(now: 3) == .trackingLost)
        board.set(.storage, to: .storageFull, now: 3)
        #expect(board.current(now: 4) == .storageFull)
        // Low Power Mode is only mentioned for a few seconds, so it never hides the guidance for a whole scan.
        board.set(.storage, to: nil, now: 5)
        board.set(.tracking, to: nil, now: 5)
        #expect(board.current(now: 5) == .lowPower)
        #expect(board.current(now: 7) == nil)
    }

    @Test func raisingTheSameAlertAgainDoesNotRestartItsClock() {
        var board = AlertBoard()
        board.set(.thermal, to: .hot, now: 0)
        board.set(.thermal, to: .hot, now: 7)
        #expect(board.current(now: 9) == nil)   // 8 s from the first raise
    }

    @Test func onlyTheTroubleThatLosesDataStopsShooting() {
        var board = AlertBoard()
        board.set(.thermal, to: .hot, now: 0)
        board.set(.power, to: .lowPower, now: 0)
        #expect(!board.pausesShooting(now: 1))
        board.set(.camera, to: .interrupted, now: 1)
        #expect(board.pausesShooting(now: 2))
        board.set(.camera, to: nil, now: 3)
        #expect(!board.pausesShooting(now: 3))
        board.set(.thermal, to: .overheating, now: 4)
        #expect(board.pausesShooting(now: 100))   // no timeout for a critical phone
    }

    @Test func everyAlertSaysWhatToDoAndWhatHappenedToTheData() {
        #expect(CaptureAlert.interrupted.text.contains("kept"))
        #expect(CaptureAlert.storageFull.text.contains("Done"))
        #expect(CaptureAlert.overheating.text.contains("Done"))
        #expect(CaptureAlert.trackingLost.text.hasPrefix("Lost its place"))
        for kind in CaptureAlert.Kind.allCases {
            let alerts: [CaptureAlert] = [.interrupted, .trackingLost, .storageFull, .overheating, .hot, .lowPower]
            #expect(alerts.contains { $0.kind == kind })
        }
    }

    @Test func trackingAdviceOnlyCallsALostPositionLost() {
        #expect(TrackingAdvice.alert(.normal, hadNormal: true) == nil)
        #expect(TrackingAdvice.alert(.limited(.relocalizing), hadNormal: false) == .trackingLost)
        #expect(TrackingAdvice.alert(.notAvailable, hadNormal: true) == .trackingLost)
        // Before the first good frame, and for motion or a bare surface, the cue says "move slowly"; no alert.
        #expect(TrackingAdvice.alert(.notAvailable, hadNormal: false) == nil)
        #expect(TrackingAdvice.alert(.limited(.initializing), hadNormal: false) == nil)
        #expect(TrackingAdvice.alert(.limited(.excessiveMotion), hadNormal: true) == nil)
        #expect(TrackingAdvice.alert(.limited(.insufficientFeatures), hadNormal: true) == nil)
    }

    @Test func phoneHealthAlerts() {
        #expect(DeviceHealth.thermal(.nominal) == nil && DeviceHealth.thermal(.fair) == nil)
        #expect(DeviceHealth.thermal(.serious) == .hot)
        #expect(DeviceHealth.thermal(.critical) == .overheating)
        #expect(DeviceHealth.storage(freeBytes: 120_000_000) == .storageFull)
        #expect(DeviceHealth.storage(freeBytes: 2_000_000_000) == nil)
        #expect(DeviceHealth.storage(freeBytes: nil) == nil)
        #expect(DeviceHealth.power(lowPowerMode: true) == .lowPower && DeviceHealth.power(lowPowerMode: false) == nil)
    }

    // MARK: One cue at a time

    @Test func oneCueWinsInPriorityOrder() {
        // An alert beats everything.
        #expect(CueArbiter.winner(alert: .interrupted, cueTone: .guide, leadShowing: true) == .alert)
        // A warning about the photo being taken beats a lead.
        #expect(CueArbiter.winner(alert: nil, cueTone: .caution, leadShowing: true) == .cue)
        #expect(CueArbiter.winner(alert: nil, cueTone: .stop, leadShowing: true) == .cue)
        // A lead beats steady guidance and the "all good" cue.
        #expect(CueArbiter.winner(alert: nil, cueTone: .guide, leadShowing: true) == .lead)
        #expect(CueArbiter.winner(alert: nil, cueTone: .good, leadShowing: true) == .lead)
        #expect(CueArbiter.winner(alert: nil, cueTone: .guide, leadShowing: false) == .cue)
        #expect(CueArbiter.winner(alert: nil, cueTone: nil, leadShowing: true) == .lead)
    }

    @Test func aWarningMustLastBeforeItIsShown() {
        var d = CueDebouncer()
        // One shaky frame does not flash "Blurry".
        #expect(d.update("Blurry: hold still", tone: .caution, now: 0) == nil)
        #expect(d.update(nil, tone: .guide, now: 0.2) == nil)
        // Still blurry for 0.8 s: now it shows.
        #expect(d.update("Blurry: hold still", tone: .caution, now: 1.0) == nil)
        #expect(d.update("Blurry: hold still", tone: .caution, now: 1.9) == "Blurry: hold still")
    }

    @Test func aShownWarningStaysLongEnoughToRead() {
        var d = CueDebouncer()
        _ = d.update("Too dark: add light", tone: .caution, now: 0)
        #expect(d.update("Too dark: add light", tone: .caution, now: 1) == "Too dark: add light")
        // The cause is gone at once, the words stay for 1.2 s after they appeared.
        #expect(d.update("Keep walking around it", tone: .guide, now: 1.5) == "Too dark: add light")
        #expect(d.update("Keep walking around it", tone: .guide, now: 2.3) == "Keep walking around it")
    }

    @Test func aStopCueBypassesTheHoldOfAnOlderWarning() {
        var d = CueDebouncer()
        _ = d.update("Too far: move closer", tone: .caution, now: 0)
        #expect(d.update("Too far: move closer", tone: .caution, now: 1) == "Too far: move closer")
        // Moved too close a moment later: the stop cue is on screen at once, not after the warning's 1.2 s.
        #expect(d.update("Too close: back up", tone: .stop, now: 1.2) == "Too close: back up")
    }

    @Test func guidanceAndStopCuesAreNotDelayed() {
        var d = CueDebouncer()
        #expect(d.update("Walk slowly around it", tone: .guide, now: 0) == "Walk slowly around it")
        #expect(d.update("Too close: back up", tone: .stop, now: 0.1) == "Too close: back up")
        #expect(d.update("Move closer here", tone: .guide, now: 0.2) == "Move closer here")
    }

    // MARK: Where to go next, in words

    @Test func walkCueSaysWhichWayRoundTheShortWay() {
        func cue(_ target: Int, ring: Int = 0, from: Int = 0, hereRing: Int = 0) -> String {
            CoverageDome.walkCue(fromSector: from, ring: hereRing, to: ring * CoverageDome.sectors + target)
        }
        #expect(cue(2) == "Walk a little to the right")
        #expect(cue(4) == "Walk right around it")
        #expect(cue(6) == "Go around to the other side")
        #expect(cue(8) == "Walk left around it")
        #expect(cue(11) == "Walk a little to the left")
        #expect(cue(0, ring: 1) == "Go higher")
        #expect(cue(0, ring: 0, hereRing: 2) == "Go lower")
        #expect(cue(4, ring: 0, hereRing: 2) == "Walk right around it and go lower")
        #expect(cue(1, from: 11) == "Walk a little to the right")   // across the wrap
        #expect(cue(0) == "Keep going")
    }

    @Test func rightIsTheDirectionOfIncreasingSector() {
        // A camera at azimuth 0 (+Z) looking at the box, then one a quarter turn on: the second is to the first's right.
        let box = ObjectBox(center: SIMD3(0, 0.05, 0), size: SIMD3(0.1, 0.1, 0.1), yaw: 0)
        let a = SIMD3<Float>(0, 0.2, 0.5), b = SIMD3<Float>(0.5, 0.2, 0)
        #expect(CoverageDome.sector(azimuth: CoverageDome.azimuth(a - box.center)) == 0)
        #expect(CoverageDome.sector(azimuth: CoverageDome.azimuth(b - box.center)) == 3)
        let look = -simd_normalize(SIMD3(a.x, 0, a.z))
        let right = SIMD3<Float>(-look.z, 0, look.x)
        #expect(simd_dot(b - a, right) > 0)
    }

    // MARK: Close-ups in words

    @Test func closeUpSidesAreNamedFromWhereThePersonStands() {
        let box = ObjectBox(center: SIMD3(0, 0.05, 0), size: SIMD3(0.1, 0.1, 0.1), yaw: 0)
        let cam = SIMD3<Float>(0, 0.2, 0.5)   // in front of the +Z face, looking toward -Z
        #expect(FaceWords.name(face: 0, box: box, camera: cam) == "the top")
        #expect(FaceWords.name(face: 3, box: box, camera: cam) == "the side facing you")
        #expect(FaceWords.name(face: 4, box: box, camera: cam) == "the far side")
        #expect(FaceWords.name(face: 1, box: box, camera: cam) == "the right side")
        #expect(FaceWords.name(face: 2, box: box, camera: cam) == "the left side")
        // Walk to the other side and the words follow.
        #expect(FaceWords.name(face: 3, box: box, camera: SIMD3(0, 0.2, -0.5)) == "the far side")
        #expect(FaceWords.name(face: 1, box: box, camera: SIMD3(0, 0.2, -0.5)) == "the left side")
    }

    @Test func theCloseUpPromptCountsTheOnesDone() {
        let box = ObjectBox(center: SIMD3(0, 0.05, 0), size: SIMD3(0.1, 0.1, 0.1), yaw: 0)
        var dome = CoverageDome()
        dome.requiredFaces = 5
        dome.coverFace(0)
        dome.coverFace(3)
        let p = CloseupPrompt.make(face: 1, box: box, camera: SIMD3(0, 0.2, 0.5), dome: dome)
        #expect(p?.sentence == "Move closer to the right side (3 of 5)")
        #expect(CloseupPrompt.make(face: 1, box: box, camera: SIMD3(0, 0.2, 0.5), dome: CoverageDome()) == nil)   // none required
        #expect(CoverageSummary.closeUps(dome) == "2 of 5 close-ups")
        #expect(CoverageSummary.closeUps(CoverageDome()) == nil)
    }

    @Test func sidesSummaryUsesTheTarget() {
        var dome = CoverageDome()
        for s in 0..<5 { dome.cover(s) }
        #expect(CoverageSummary.sides(dome) == "5 of 8 sides")
        for s in 5..<12 { dome.cover(s) }
        #expect(CoverageSummary.sides(dome) == "8 of 8 sides")   // never more than the target
    }

    @Test func aRetakenViewIsOpenAgain() {
        var dome = CoverageDome()
        dome.cover(3)
        dome.coverFace(2)
        dome.uncover(3)
        dome.uncoverFace(2)
        #expect(!dome.isCovered(3) && !dome.isFaceCovered(2))
    }

    // MARK: Without LiDAR

    private func fullDome(faces: Bool = false) -> CoverageDome {
        var d = CoverageDome()
        for s in 0..<8 { d.cover(s) }
        for s in 0..<2 { d.cover(2 * CoverageDome.sectors + s) }
        if faces {
            d.requiredFaces = 5
            for f in 0..<5 { d.coverFace(f) }
        }
        return d
    }

    @Test func aPhoneWithoutLiDARCanReachEnough() {
        // No depth keyframes ever: the mesh share would cap progress at 65 %, and the scan could never finish.
        let stuck = ScanProgress.main(dome: fullDome(), keyframeSectors: 0)
        #expect(stuck.value < 0.7 && !stuck.enough)
        let photosOnly = ScanProgress.main(dome: fullDome(), keyframeSectors: 0, lidar: false)
        #expect(photosOnly.value == 1 && photosOnly.enough)
        let withFaces = ScanProgress.main(dome: fullDome(faces: true), keyframeSectors: 0, lidar: false)
        #expect(abs(withFaces.value - 1) < 1e-9 && withFaces.enough)
        // Half the views: half the number, not a share of 65 %.
        var half = CoverageDome()
        for s in 0..<4 { half.cover(s) }
        #expect(abs(ScanProgress.main(dome: half, keyframeSectors: 0, lidar: false).value - 0.4) < 1e-9)
        // Missing faces still hold it back.
        var partial = fullDome(faces: false)
        partial.requiredFaces = 5
        partial.coverFace(0)
        #expect(!ScanProgress.main(dome: partial, keyframeSectors: 0, lidar: false).enough)
    }

    /// A camera 50 cm in front of and 15 cm above a small object at the origin, looking at its middle. The viewing ray
    /// goes on to hit the table 25 cm BEHIND the object, so a box placed at that hit would miss the object.
    private let rayOrigin = SIMD3<Float>(0, 0.15, 0.5)
    private var rayDirection: SIMD3<Float> { simd_normalize(SIMD3(0, 0.05, 0) - rayOrigin) }
    private var tableHit: SIMD3<Float> {
        let t = (0 - rayOrigin.y) / rayDirection.y
        return rayOrigin + rayDirection * t
    }

    @Test func theBoxComesFromFeaturePointsOnTheViewingRayWhenThereIsNoDepth() {
        var points: [SIMD3<Float>] = []
        for i in 0..<12 {
            let a = Float(i) * 0.5
            points.append(SIMD3(0.04 * cos(a), 0.03 + 0.005 * Float(i), 0.06 * sin(a)))
        }
        // A point on a wall to the side and the table's own point are ignored.
        points.append(SIMD3(0.19, 0.20, 0))
        points.append(SIMD3(0, 0.0, 0))
        let fit = BoxFitter.fitSparse(featurePoints: points, rayOrigin: rayOrigin, rayDirection: rayDirection, tap: tableHit, planeY: 0)
        #expect(tableHit.z < -0.2)   // the table hit really is far behind the object
        #expect(fit.measured)
        #expect(fit.pointsUsed >= BoxFitter.sparseMinPoints)
        #expect(abs(fit.box.planeY) < 1e-5)
        #expect(simd_distance(SIMD2(fit.box.center.x, fit.box.center.z), SIMD2(0, 0)) < 0.05)
        #expect(points.prefix(12).allSatisfy { fit.box.contains($0, margin: 0.02) })
    }

    @Test func withTooFewPointsTheBoxIsADefaultWhereTheRayMeetsTheObjectAndSaysSo() {
        let fit = BoxFitter.fitSparse(featurePoints: [SIMD3(0.01, 0.05, 0), SIMD3(-0.01, 0.06, 0.02)], rayOrigin: rayOrigin,
                                      rayDirection: rayDirection, tap: tableHit, planeY: 0)
        #expect(!fit.measured)
        #expect(fit.box.size == BoxFitter.assumedSize)
        // Where the ray crosses the middle of a default-height object, not at the table hit behind it.
        #expect(abs(fit.box.center.x) < 1e-4 && abs(fit.box.center.z) < 1e-3 && abs(fit.box.planeY) < 1e-6)
        #expect(!BoxFitter.fitSparse(featurePoints: [], rayOrigin: rayOrigin, rayDirection: rayDirection, tap: tableHit, planeY: 0).measured)
        // A ray that points level has no object height to cross: the table hit is used.
        let level = BoxFitter.fitSparse(featurePoints: [], rayOrigin: SIMD3(0, 0.3, 1), rayDirection: SIMD3(0, 0, -1),
                                        tap: SIMD3(0.2, 0, -1), planeY: 0)
        #expect(level.box.center.x == 0.2 && level.box.center.z == -1)
    }

    @Test func aRetakenPhotoThatStaysOnFileStopsCountingTowardTheCap() {
        let cap = CaptureSelection.maxStills
        // At the cap with 4 close-up sides still to come, there is no room; retaking one frees one slot.
        #expect(!CaptureSelection.hasRoomForOrbit(kept: cap - 4, retaken: 0, facesLeft: 4))
        #expect(CaptureSelection.hasRoomForOrbit(kept: cap - 4, retaken: 1, facesLeft: 4))
        #expect(!CaptureSelection.hasRoomForClose(kept: cap, retaken: 0))
        #expect(CaptureSelection.hasRoomForClose(kept: cap, retaken: 1))
    }

    // MARK: A capture that was cut off

    @Test func anEarlierRunsFilesAreCountedSoTheNextRunDoesNotOverwriteThem() throws {
        let root = FileManager.default.temporaryDirectory.appending(path: "lea_existing_\(UUID().uuidString)")
        let stills = root.appending(path: "stills"), scan = root.appending(path: "scans/scan_a")
        try FileManager.default.createDirectory(at: stills, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: scan, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        for name in ["scan_a_s1.json", "scan_a_s1.jpg", "scan_a_s12.json", "scan_a_s12.jpg", "scan_a_s12_depth.png", "scan_b_s99.json"] {
            try Data().write(to: stills.appending(path: name))
        }
        for name in ["kf_0000.jpg", "kf_0000_depth.png", "kf_0007.jpg", "kf_0007_conf.png", "points_arkit_world.ply"] {
            try Data().write(to: scan.appending(path: name))
        }
        let found = ExistingCapture.find(stillsDir: stills, scanDir: scan, scanID: "scan_a")
        #expect(found.highestStill == 12 && found.keyframeCount == 8 && !found.isEmpty)
        let empty = ExistingCapture.find(stillsDir: root.appending(path: "none"), scanDir: root.appending(path: "none"), scanID: "scan_a")
        #expect(empty.isEmpty)
    }
}
