import Foundation
import SceneKit

/// Keeps the numbered markers on the 3D view from covering each other. Works in screen space: markers are placed
/// in rank order, the first never moves, and each later one is pushed straight away from any earlier marker it
/// would overlap until they are `gap` points apart. Pure, so it is unit-tested (MarkerLayoutTests).
enum MarkerLayout {
    /// Offsets (screen points) to add to `points` (rank order) so every pair is at least `gap` apart.
    static func separate(_ points: [CGPoint], gap: CGFloat) -> [CGVector] {
        var placed: [CGPoint] = []
        var out: [CGVector] = []
        for (i, start) in points.enumerated() {
            var p = start
            // Two passes settle the rare case where a push moves it onto another earlier marker.
            for _ in 0..<2 {
                for q in placed {
                    let dx = p.x - q.x, dy = p.y - q.y
                    let d = (dx * dx + dy * dy).squareRoot()
                    guard d < gap else { continue }
                    // Same spot: fan out alternately up-right and down-left so the number order stays readable.
                    let (ux, uy): (CGFloat, CGFloat) = d > 0.5 ? (dx / d, dy / d) : (i % 2 == 1 ? (0.7071, -0.7071) : (-0.7071, 0.7071))
                    p = CGPoint(x: q.x + ux * gap, y: q.y + uy * gap)
                }
            }
            placed.append(p)
            out.append(CGVector(dx: p.x - start.x, dy: p.y - start.y))
        }
        return out
    }
}

/// Applies `MarkerLayout` to the marker nodes every rendered frame, on SceneKit's render thread (3 projections a
/// frame; nothing else). The nodes keep their true 3D spot as `base`; only their drawn position is nudged.
final class MarkerSeparator: NSObject, SCNSceneRendererDelegate, @unchecked Sendable {
    private struct Entry { let node: SCNNode; let base: SIMD3<Float>; let size: Float }
    private let lock = NSLock()
    nonisolated(unsafe) private var entries: [Entry] = []
    /// Minimum distance between marker centres, as a multiple of the drawn marker diameter.
    private let spacing: CGFloat = 1.08

    /// The markers in rank order (marker 1 first). `size` is the marker's world-space width.
    @MainActor func set(_ nodes: [(node: SCNNode, size: Float)]) {
        let entries = nodes.map { Entry(node: $0.node, base: $0.node.simdPosition, size: $0.size) }
        lock.withLock { self.entries = entries }
    }

    func renderer(_ renderer: SCNSceneRenderer, updateAtTime time: TimeInterval) {
        let entries = lock.withLock { self.entries }
        guard entries.count > 1, let parent = entries[0].node.parent, let pov = renderer.pointOfView else { return }
        let space = parent.presentation
        let right = pov.presentation.simdWorldRight
        var screen: [CGPoint] = []
        var depth: [Float] = []
        var gap: CGFloat = 0
        for e in entries {
            let world = space.simdConvertPosition(e.base, to: nil)
            let p = renderer.projectPoint(SCNVector3(world))
            let edge = renderer.projectPoint(SCNVector3(world + right * e.size))
            screen.append(CGPoint(x: CGFloat(p.x), y: CGFloat(p.y)))
            depth.append(p.z)
            gap = max(gap, CGFloat(abs(edge.x - p.x)) * 2 * spacing)
        }
        let offsets = MarkerLayout.separate(screen, gap: gap)
        for (i, e) in entries.enumerated() {
            let o = offsets[i]
            if o.dx == 0 && o.dy == 0 {
                if e.node.simdPosition != e.base { e.node.simdPosition = e.base }
                continue
            }
            let moved = renderer.unprojectPoint(SCNVector3(Float(screen[i].x + o.dx), Float(screen[i].y + o.dy), depth[i]))
            e.node.simdPosition = space.simdConvertPosition(SIMD3(moved), from: nil)
        }
    }
}
