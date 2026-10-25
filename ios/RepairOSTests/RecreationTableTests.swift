import Foundation
import simd
import Testing
@testable import RepairOS

/// The report's 3D model shows the object alone: the phone's shell cuts its table sheet from the mesh (ShellSurface
/// cutTable, the gateway's recreation/surface.py cut_table), and a gateway model closed underneath by a registered
/// flipped segment lets the viewer turn it over (RecreationManifest.closedBottom).
struct RecreationTableTests {
    /// A wavy 30 cm table sheet (±1.5 mm) at y = 0 with a 6 x 2 x 4 cm box standing on it: walls and a lid.
    static func scene() -> ([SIMD3<Float>], [UInt32]) {
        var v: [SIMD3<Float>] = [], t: [UInt32] = []
        func quad(_ a: SIMD3<Float>, _ b: SIMD3<Float>, _ c: SIMD3<Float>, _ d: SIMD3<Float>) {
            let i = UInt32(v.count)
            v += [a, b, c, d]
            t += [i, i + 1, i + 2, i, i + 2, i + 3]
        }
        let step: Float = 0.01
        for i in -15..<15 { for k in -15..<15 {
            let x = Float(i) * step, z = Float(k) * step
            func y(_ x: Float, _ z: Float) -> Float { 0.0015 * sin(x * 90) * cos(z * 70) }
            quad([x, y(x, z), z], [x, y(x, z + step), z + step], [x + step, y(x + step, z + step), z + step],
                 [x + step, y(x + step, z), z])
        } }
        let lo = SIMD3<Float>(-0.03, 0, -0.02), hi = SIMD3<Float>(0.03, 0.02, 0.02)
        for s in 0..<10 {   // walls in 2 mm bands, so the lowest bands sit inside the cut
            let y0 = Float(s) * 0.002, y1 = y0 + 0.002
            quad([lo.x, y0, lo.z], [hi.x, y0, lo.z], [hi.x, y1, lo.z], [lo.x, y1, lo.z])
            quad([lo.x, y0, hi.z], [lo.x, y1, hi.z], [hi.x, y1, hi.z], [hi.x, y0, hi.z])
            quad([lo.x, y0, lo.z], [lo.x, y1, lo.z], [lo.x, y1, hi.z], [lo.x, y0, hi.z])
            quad([hi.x, y0, lo.z], [hi.x, y0, hi.z], [hi.x, y1, hi.z], [hi.x, y1, lo.z])
        }
        quad([lo.x, hi.y, lo.z], [lo.x, hi.y, hi.z], [hi.x, hi.y, hi.z], [hi.x, hi.y, lo.z])
        return (v, t)
    }

    @Test func cutsTheTableSheetAndKeepsTheObject() {
        let (v, t) = Self.scene()
        let kept = ShellSurface.cutTable(v, t, groundY: 0)
        var lid = 0, walls = 0, low = 0
        for f in Swift.stride(from: 0, to: kept.count, by: 3) {
            let c = (v[Int(kept[f])] + v[Int(kept[f + 1])] + v[Int(kept[f + 2])]) / 3
            if c.y > 0.0199 { lid += 1 } else if c.y > 0.004 { walls += 1 } else { low += 1 }
            #expect(abs(c.x) <= 0.031 && abs(c.z) <= 0.021)     // nothing of the table outside the box remains
        }
        #expect(lid == 2)
        #expect(walls >= 30)                                   // the sides stand from the table up
        #expect(kept.count < t.count / 4)
        _ = low
    }

    @Test func leavesTheMeshAloneWithoutAGround() {
        let (v, t) = Self.scene()
        #expect(ShellSurface.cutTable(v, t, groundY: nil) == t)
    }

    @Test func aRegisteredBottomLetsTheViewerTurnTheModelOver() {
        var m = RecreationManifest(kind: "textured_mesh", origin: "gateway", rev: 2, file: "model.usdz", texture: nil,
                                   focusCenter: nil, focusRadius: nil)
        #expect(!m.closedBottom)
        m.stats[RecreationManifest.bottomKey] = 1
        #expect(m.closedBottom)
        let round = try? JSONDecoder().decode(RecreationManifest.self, from: JSONEncoder().encode(m))
        #expect(round?.closedBottom == true)
    }
}
