import Foundation
import simd
import Testing
@testable import RepairOS

/// Lea moving the report's 3D model while she answers (Report/Stage/LeaStage): the gateway's J events decoded, what
/// the report does with each (focus, a neutral ring, a camera move), the camera poses for sides and zoom, and when a
/// move lands so it goes with its sentence.
struct LeaStageTests {
    static func event(_ o: [String: Any]) -> ConverseEvent? {
        GatewayClient.event(UInt8(ascii: "J"), try! JSONSerialization.data(withJSONObject: o))
    }

    static func stage(_ o: [String: Any]) -> StageCommand? {
        if case .stage(let c) = event(o) { return c }
        return nil
    }

    @Test func decodesEveryMoveAndDropsMalformedOnes() {
        #expect(Self.stage(["type": "show", "part": "f_ab12"]) == .show("f_ab12"))
        #expect(Self.stage(["type": "view", "side": "bottom"]) == .view(.bottom))
        #expect(Self.stage(["type": "view", "side": "around"]) == .view(.around))
        #expect(Self.stage(["type": "zoom", "dir": "in"]) == .zoom(in: true))
        #expect(Self.stage(["type": "zoom", "dir": "out"]) == .zoom(in: false))
        let p = Self.stage(["type": "point", "id": "ix_3", "x": 0.1, "y": -0.2, "z": 0.3, "label": "A2", "radius": 0.02])
        #expect(p == .point(id: "ix_3", at: SIMD3(0.1, -0.2, 0.3), label: "A2", radius: 0.02))
        #expect(Self.stage(["type": "point", "id": "pt_1", "x": 0, "y": 0, "z": 0]) == .point(id: "pt_1", at: .zero, label: "", radius: 0.015))
        #expect(Self.stage(["type": "view", "side": "sideways"]) == nil)
        #expect(Self.stage(["type": "zoom", "dir": "far"]) == nil)
        #expect(Self.stage(["type": "show", "part": ""]) == nil)
        #expect(Self.stage(["type": "point", "id": "pt_1", "x": 0.1]) == nil)
    }

    @Test func decodesTheSpotPhoto() {
        guard case .photo(let id, let crop) = Self.event(["type": "photo", "for": "f_ab12", "crop": "abc123"]) else {
            Issue.record("no photo event"); return
        }
        #expect(id == "f_ab12" && crop == "abc123")
        #expect(Self.event(["type": "photo", "for": "f_ab12"]) == nil)
    }

    static func problem(_ id: String, point: SIMD3<Float>?, extent: Float? = nil) -> ReportModel.Problem {
        ReportModel.Problem(id: id, part: "Part", issue: ReportModel.noIssue, evidence: [], crops: [], photoLabels: [],
                            regions: 1, point: point, extent: extent, proved: false)
    }

    @Test func showFocusesANumberedMarkerOrRingsAPartWithNone() {
        let pins = [ModelPin(id: "f_top", number: 1, point: SIMD3(0, 0, 0), tone: .danger)]
        let located = [Self.problem("f_top", point: .zero), Self.problem("f_fine", point: SIMD3(0.1, 0, 0), extent: 0.004),
                       Self.problem("f_nowhere", point: nil)]
        #expect(LeaStagePlan.make(.show("f_top"), pins: pins, located: located) == LeaStagePlan(focus: "f_top", camera: .show("f_top")))
        let fine = LeaStagePlan.make(.show("f_fine"), pins: pins, located: located)
        #expect(fine.focus == "lea:f_fine" && fine.camera == .show("lea:f_fine"))
        #expect(fine.pin?.tone == .neutral && fine.pin?.point == SIMD3(0.1, 0, 0) && fine.pin?.ringRadius == 0.012)
        #expect(LeaStagePlan.make(.show("f_nowhere"), pins: pins, located: located) == .none)   // never shown: ignored
        #expect(LeaStagePlan.make(.show("f_made_up"), pins: pins, located: located) == .none)
    }

    @Test func aPointBecomesARingSizedToItsUncertainty() {
        let plan = LeaStagePlan.make(.point(id: "ix_2", at: SIMD3(0, 0.1, 0), label: "A2", radius: 0.2), pins: [], located: [])
        #expect(plan.focus == "lea:ix_2" && plan.pin?.ringRadius == 0.05 && plan.camera == .show("lea:ix_2"))
        let tight = LeaStagePlan.make(.point(id: "pt_1", at: .zero, label: "", radius: 0.001), pins: [], located: [])
        #expect(tight.pin?.ringRadius == 0.008)
        #expect(LeaStagePlan.make(.point(id: "pt_2", at: SIMD3(.nan, 0, 0), label: "", radius: 0.01), pins: [], located: []) == .none)
        #expect(LeaStagePlan.make(.view(.top), pins: [], located: []) == LeaStagePlan(camera: .view(.top)))
        #expect(LeaStagePlan.make(.zoom(in: true), pins: [], located: []).focus == nil)
    }

    @Test func sidesAreAroundTheCentreFromTheDefaultFront() {
        let c = SIMD3<Float>(1, 0, 2), now = TourPose(eye: SIMD3(1, 0, 3), look: c)
        func pose(_ s: StageSide) -> TourPose { StageCamera.pose(s, current: now, center: c, distance: 2, front: 0)! }
        #expect(abs(simd_distance(pose(.front).eye, c) - 2) < 1e-4 && pose(.front).look == c)
        #expect(pose(.front).eye.z > c.z + 1.5 && pose(.back).eye.z < c.z - 1.5)
        #expect(pose(.left).eye.x < c.x - 1.5 && pose(.right).eye.x > c.x + 1.5)    // the viewer's left and right
        #expect(pose(.top).eye.y > 1.9 && pose(.bottom).eye.y < -1.9)
        #expect(abs(asin((pose(.top).eye.y - c.y) / 2)) < Float(82) * .pi / 180)  // inside the orbit limits
        #expect(StageCamera.pose(.reset, current: now, center: c, distance: 2, front: 0) == nil)
        let around = StageCamera.pose(.around, current: now, center: c, distance: 9, front: 0)!
        #expect(simd_distance(around.eye, SIMD3(1, 0, 1)) < 1e-4)                   // same distance, other side
        let rotated = StageCamera.pose(.front, current: now, center: .zero, distance: 1, front: .pi / 2)!
        #expect(rotated.eye.x > 0.9)                                                // "front" follows the framing
    }

    @Test func zoomStepsInAndOutWithinLimits() {
        let p = TourPose(eye: SIMD3(0, 0, 1), look: .zero)
        #expect(abs(simd_length(StageCamera.zoom(p, zoomIn: true, minDistance: 0.1, maxDistance: 5).eye) - 0.6) < 1e-5)
        #expect(abs(simd_length(StageCamera.zoom(p, zoomIn: false, minDistance: 0.1, maxDistance: 5).eye) - 1 / 0.6) < 1e-4)
        #expect(simd_length(StageCamera.zoom(p, zoomIn: true, minDistance: 0.9, maxDistance: 5).eye) == 0.9)
        #expect(simd_length(StageCamera.zoom(p, zoomIn: false, minDistance: 0.1, maxDistance: 1.2).eye) == 1.2)
    }

    @Test func aMoveWaitsForItsSentenceToBeHeard() {
        let t0 = Date(timeIntervalSince1970: 1000)
        #expect(StageTiming.delay(firstHeardAt: nil, charsBefore: 200, now: t0) == 0)
        #expect(StageTiming.delay(firstHeardAt: t0, charsBefore: 0, now: t0) == 0)
        #expect(abs(StageTiming.delay(firstHeardAt: t0, charsBefore: 70, now: t0.addingTimeInterval(1)) - 4) < 1e-9)
        #expect(StageTiming.delay(firstHeardAt: t0, charsBefore: 14, now: t0.addingTimeInterval(9)) == 0)
    }

    @Test func locatedPartsIncludeTheOnesWithNoIssue() {
        func f(_ id: String, _ issue: String, located: Bool) -> [String: Any] {
            ["finding_id": id, "part_class": "button", "issue": issue,
             "location": located ? ["status": "computed", "point_m": [0.1, 0.2, 0.3]] as [String: Any]
                                 : ["status": "not_computed"] as [String: Any]]
        }
        let r = ReportModel(json: ["verdict": "no_problems_seen",
                                   "findings": [f("f_b", "none", located: true), f("f_x", "none", located: false)]])
        #expect(r.located.map(\.id) == ["f_b"] && r.mostLikely.isEmpty)
    }
}
