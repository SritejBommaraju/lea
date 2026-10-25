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

    /// After the table cut, a cable lying beside the body (joined to it only through the table) is a piece of its own.
    /// It stays with the body like on the gateway (keep_central); a speck and a neighbour beyond the gap go.
    @Test func keepsTheObjectsNearbyPiecesAndDropsSpecksAndNeighbours() {
        var v: [SIMD3<Float>] = [], t: [UInt32] = []
        func patch(_ o: SIMD3<Float>, _ du: SIMD3<Float>, _ dv: SIMD3<Float>, _ n: Int) {   // n x n quads, one piece
            let base = UInt32(v.count)
            for i in 0...n { for j in 0...n { v.append(o + du * (Float(i) / Float(n)) + dv * (Float(j) / Float(n))) } }
            for i in 0..<n { for j in 0..<n {
                let a = base + UInt32(i * (n + 1) + j), b = a + UInt32(n + 1)
                t += [a, b, b + 1, a, b + 1, a + 1]
            } }
        }
        func lid(_ x: Float) { patch([x - 0.03, 0.02, -0.02], [0.06, 0, 0], [0, 0, 0.04], 20) }
        lid(0)                                                                  // the body: 800 triangles
        patch([0.035, 0.016, -0.0025], [0.08, 0, 0], [0, 0, 0.005], 6)          // a cable about 8 mm off it: 72
        patch([-0.045, 0.003, 0], [0.003, 0, 0], [0, 0, 0.003], 1)              // a speck 1.5 cm off: 2
        lid(0.25)                                                               // a neighbour 19 cm off: 800
        let box = ShellBox(center: [0, 0.01, 0], extent: [0.07, 0.025, 0.05], rotation: matrix_identity_float3x3)
        let (kv, kt) = ShellSurface.keepCentral(v, t, box: box)
        #expect(kt.count / 3 == 800 + 72)
        #expect(kv.contains { $0.x > 0.1 && $0.x < 0.12 })                     // the cable's far end
        #expect(kv.allSatisfy { $0.x > -0.035 && $0.x < 0.12 })                 // no speck, no neighbour
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
