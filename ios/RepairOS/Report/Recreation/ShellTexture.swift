import CoreGraphics
import Foundation
import ImageIO
import simd
import UniformTypeIdentifiers

// Tier 1 texture: the surface textured from the scan's own photos (the 12 MP stills; a keyframe only where no still
// sees a face). The same method as the gateway's `repairos/recreation/texture.py` (keep the two in step), after
// "Let There Be Color!" (Waechter, Moehrle, Goesele, ECCV 2014):
//   per face the view that shows it largest and sharpest (projected area in pixels x the view's sharpness), visible
//   (a z-buffer of the mesh from that view, and the view's own LiDAR depth for things not in the mesh: a hand),
//   smoothed so neighbours share a view (iterated conditional modes); each connected run of faces with one view is a
//   chart whose tile in the atlas is the chart's rectangle of that photo, scaled (so the pixels around a chart are the
//   real neighbouring pixels); one gain per photo levels exposure and white balance across seams (least squares), and
//   a smooth local correction spread from the seam vertices (push-pull) removes what is left (Waechter's local seam
//   levelling), so no seam shows while each face keeps one photo's full detail.

struct ShellView: Sendable {
    var name: String
    var isStill: Bool
    var image: URL
    var pose: CameraPose
    var cam: Intrinsics              // at the image's own size
    var depth: DepthFrameData?       // its LiDAR depth (the nearest regular frame's for a still)
    var sharpness: Float = 1
    var weight: Float = 1
}

/// A Sendable box for buffers written from `concurrentPerform` workers at disjoint indices.
final class SharedBuffer<T>: @unchecked Sendable {
    var a: [T]
    init(_ a: [T]) { self.a = a }
}

enum ShellTexture {
    static let edgeMargin: Float = 0.01
    static let minFacing: Float = 0.15
    static let occluder: Float = 0.03
    static let smoothWeight: Float = 1.0
    static let smoothIterations = 6
    static let keyframePenalty: Float = 0.35
    static let tilePad = 2
    static let seamReg: Float = 0.05
    static let gainLimits: ClosedRange<Float> = 0.75...1.33
    static let localLimit: Float = 60
    static let unseenTile = 16
    static let thumbWidth = 504

    struct Output {
        var mesh: TexturedMesh
        var atlas: CGImage
        var stats: [String: Double]
    }

    // MARK: Images

    static func thumbnail(_ url: URL, maxPixel: Int) -> (pixels: [UInt8], width: Int, height: Int)? {
        guard let src = CGImageSourceCreateWithURL(url as CFURL, nil),
              let img = CGImageSourceCreateThumbnailAtIndex(src, 0, [
                kCGImageSourceThumbnailMaxPixelSize: maxPixel, kCGImageSourceCreateThumbnailFromImageAlways: true,
                kCGImageSourceCreateThumbnailWithTransform: false] as CFDictionary) else { return nil }
        let w = img.width, h = img.height
        var px = [UInt8](repeating: 0, count: w * h * 4)
        let ok = px.withUnsafeMutableBytes { raw -> Bool in
            guard let ctx = CGContext(data: raw.baseAddress, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w * 4,
                                      space: CGColorSpace(name: CGColorSpace.sRGB)!,
                                      bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue) else { return false }
            ctx.draw(img, in: CGRect(x: 0, y: 0, width: w, height: h))
            return true
        }
        return ok ? (px, w, h) : nil
    }

    /// The image decoded so that its longer side is at most `maxPixel` (ImageIO's subsampled JPEG decode).
    static func scaledImage(_ url: URL, maxPixel: Int) -> CGImage? {
        guard let src = CGImageSourceCreateWithURL(url as CFURL, nil) else { return nil }
        return CGImageSourceCreateThumbnailAtIndex(src, 0, [
            kCGImageSourceThumbnailMaxPixelSize: max(64, maxPixel), kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: false, kCGImageSourceShouldCacheImmediately: true] as CFDictionary)
    }

    static func fullImage(_ url: URL) -> CGImage? {
        guard let src = CGImageSourceCreateWithURL(url as CFURL, nil) else { return nil }
        return CGImageSourceCreateImageAtIndex(src, 0, [kCGImageSourceShouldCacheImmediately: true] as CFDictionary)
    }

    static func imageSize(_ url: URL) -> (Int, Int)? {
        guard let src = CGImageSourceCreateWithURL(url as CFURL, nil),
              let p = CGImageSourceCopyPropertiesAtIndex(src, 0, nil) as? [CFString: Any],
              let w = p[kCGImagePropertyPixelWidth] as? Int, let h = p[kCGImagePropertyPixelHeight] as? Int else { return nil }
        return (w, h)
    }

    /// Variance of the Laplacian of the grey image (about 640 px wide) inside the box's projection.
    static func sharpness(_ v: ShellView, box: ShellBox) -> Float {
        guard let (px, w, h) = thumbnail(v.image, maxPixel: 640) else { return 1 }
        var x0 = 0, y0 = 0, x1 = w, y1 = h
        let cam = v.cam.scaled(to: w, h)
        var lo = SIMD2<Float>(repeating: .infinity), hi = -lo, front = true
        for sx: Float in [-0.5, 0.5] { for sy: Float in [-0.5, 0.5] { for sz: Float in [-0.5, 0.5] {
            let p = box.rotation * (SIMD3(sx, sy, sz) * box.extent) + box.center
            let (uv, z) = cam.project(v.pose.toCamera(p))
            if z <= 0.05 { front = false }
            lo = simd_min(lo, uv); hi = simd_max(hi, uv)
        } } }
        if front {
            let a = simd_clamp(lo, .zero, SIMD2(Float(w - 1), Float(h - 1))), b = simd_clamp(hi, .zero, SIMD2(Float(w), Float(h)))
            if b.x - a.x > 16, b.y - a.y > 16 { x0 = Int(a.x); y0 = Int(a.y); x1 = Int(b.x); y1 = Int(b.y) }
        }
        func g(_ x: Int, _ y: Int) -> Float {
            let i = (y * w + x) * 4
            return 0.299 * Float(px[i]) + 0.587 * Float(px[i + 1]) + 0.114 * Float(px[i + 2])
        }
        var sum: Double = 0, sum2: Double = 0, n: Double = 0
        for y in max(1, y0)..<min(h - 1, y1) {
            for x in max(1, x0)..<min(w - 1, x1) {
                let l = Double(4 * g(x, y) - g(x - 1, y) - g(x + 1, y) - g(x, y - 1) - g(x, y + 1))
                sum += l; sum2 += l * l; n += 1
            }
        }
        guard n > 0 else { return 1 }
        return Float(sum2 / n - (sum / n) * (sum / n))
    }

    // MARK: Mesh topology

    /// Per face, the face across each edge (−1 at a border or a non-manifold edge).
    static func adjacency(_ t: [UInt32]) -> [SIMD3<Int32>] {
        let m = t.count / 3
        var adj = [SIMD3<Int32>](repeating: SIMD3(repeating: -1), count: m)
        var owner: [UInt64: Int32] = [:]
        var count: [UInt64: Int] = [:]
        owner.reserveCapacity(m * 2)
        for f in 0..<m { for e in 0..<3 {
            let a = t[3 * f + e], b = t[3 * f + (e + 1) % 3]
            let key = UInt64(min(a, b)) << 32 | UInt64(max(a, b))
            count[key, default: 0] += 1
        } }
        var first: [UInt64: (Int32, Int)] = [:]
        for f in 0..<m { for e in 0..<3 {
            let a = t[3 * f + e], b = t[3 * f + (e + 1) % 3]
            let key = UInt64(min(a, b)) << 32 | UInt64(max(a, b))
            guard count[key] == 2 else { continue }
            if let (g, ge) = first[key] {
                adj[f][e] = g
                adj[Int(g)][ge] = Int32(f)
            } else {
                first[key] = (Int32(f), e)
            }
        } }
        _ = owner
        return adj
    }

    // MARK: Visibility + scores

    /// F x V scores (row-major per face): projected area in pixels x sharpness x weight; 0 = not seen.
    static func scores(_ v: [SIMD3<Float>], _ t: [UInt32], views: [ShellView], voxel: Float) -> [Float] {
        let m = t.count / 3, nv = views.count
        let out = SharedBuffer([Float](repeating: 0, count: m * nv))
        var centre = [SIMD3<Float>](repeating: .zero, count: m), normal = centre
        for f in 0..<m {
            let a = v[Int(t[3 * f])], b = v[Int(t[3 * f + 1])], c = v[Int(t[3 * f + 2])]
            centre[f] = (a + b + c) / 3
            let n = simd_cross(b - a, c - a)
            normal[f] = simd_length(n) > 1e-12 ? simd_normalize(n) : .zero
        }
        DispatchQueue.concurrentPerform(iterations: nv) { j in
            let view = views[j]
            let cam = view.cam
            let w = Float(cam.width), h = Float(cam.height)
            // z-buffer of the mesh from this view at about 320 px
            let zs = 320 / max(w, h)
            let zw = max(1, Int(w * zs)), zh = max(1, Int(h * zs))
            let zcam = cam.scaled(to: zw, zh)
            var zbuf = [Float](repeating: .infinity, count: zw * zh)
            var proj = [SIMD2<Float>](repeating: .zero, count: v.count), depth = [Float](repeating: 0, count: v.count)
            var zproj = proj
            for i in 0..<v.count {
                let pc = view.pose.toCamera(v[i])
                let (uv, z) = cam.project(pc)
                proj[i] = uv; depth[i] = z
                zproj[i] = uv * zs
            }
            for f in 0..<m {
                let ia = Int(t[3 * f]), ib = Int(t[3 * f + 1]), ic = Int(t[3 * f + 2])
                guard depth[ia] > 0.05, depth[ib] > 0.05, depth[ic] > 0.05 else { continue }
                let pa = zproj[ia], pb = zproj[ib], pc = zproj[ic]
                let lo = simd_max(simd_min(simd_min(pa, pb), pc).rounded(.down), .zero)
                let hi = simd_min(simd_max(simd_max(pa, pb), pc).rounded(.up), SIMD2(Float(zw - 1), Float(zh - 1)))
                guard lo.x <= hi.x, lo.y <= hi.y else { continue }
                let area = (pb.x - pa.x) * (pc.y - pa.y) - (pc.x - pa.x) * (pb.y - pa.y)
                guard abs(area) > 1e-9 else { continue }
                for y in Int(lo.y)...Int(hi.y) {
                    for x in Int(lo.x)...Int(hi.x) {
                        let p = SIMD2(Float(x) + 0.5, Float(y) + 0.5)
                        let w0 = ((pb.x - p.x) * (pc.y - p.y) - (pc.x - p.x) * (pb.y - p.y)) / area
                        let w1 = ((pc.x - p.x) * (pa.y - p.y) - (pa.x - p.x) * (pc.y - p.y)) / area
                        let w2 = 1 - w0 - w1
                        guard w0 >= -0.01, w1 >= -0.01, w2 >= -0.01 else { continue }
                        let z = w0 * depth[ia] + w1 * depth[ib] + w2 * depth[ic]
                        let zi = y * zw + x
                        if z < zbuf[zi] { zbuf[zi] = z }
                    }
                }
            }
            let mx = edgeMargin * w, my = edgeMargin * h
            for f in 0..<m {
                let ia = Int(t[3 * f]), ib = Int(t[3 * f + 1]), ic = Int(t[3 * f + 2])
                guard depth[ia] > 0.05, depth[ib] > 0.05, depth[ic] > 0.05 else { continue }
                let pa = proj[ia], pb = proj[ib], pc = proj[ic]
                let lo = simd_min(simd_min(pa, pb), pc), hi = simd_max(simd_max(pa, pb), pc)
                guard lo.x > mx, lo.y > my, hi.x < w - mx, hi.y < h - my else { continue }
                let ray = centre[f] - view.pose.position
                let dist = simd_length(ray)
                let facing = -simd_dot(normal[f], ray) / max(dist, 1e-9)
                guard facing > minFacing else { continue }
                let (cuv, cz) = cam.project(view.pose.toCamera(centre[f]))
                let zx = min(zw - 1, max(0, Int(cuv.x * zs))), zy = min(zh - 1, max(0, Int(cuv.y * zs)))
                guard cz <= zbuf[zy * zw + zx] + max(2 * voxel, 0.01 * cz) else { continue }
                if let d = view.depth {
                    let du = Int(d.cam.cx + d.cam.fx * (cuv.x - cam.cx) / cam.fx)
                    let dv = Int(d.cam.cy + d.cam.fy * (cuv.y - cam.cy) / cam.fy)
                    if du >= 0, du < d.width, dv >= 0, dv < d.height {
                        let dd = d.depth[dv * d.width + du]
                        if dd > 0 && dd < cz - occluder { continue }
                    }
                }
                let area = 0.5 * abs((pb.x - pa.x) * (pc.y - pa.y) - (pc.x - pa.x) * (pb.y - pa.y))
                out.a[f * nv + j] = area * view.sharpness * view.weight
            }
        }
        return out.a
    }

    /// Per face the chosen view (−1 = none): the best, then ICM on −log(score / best) + smoothWeight per neighbour
    /// with another view.
    static func select(_ scores: [Float], faces m: Int, views nv: Int, adj: [SIMD3<Int32>]) -> [Int32] {
        var best = [Float](repeating: 0, count: m)
        var label = [Int32](repeating: -1, count: m)
        for f in 0..<m {
            for j in 0..<nv where scores[f * nv + j] > best[f] { best[f] = scores[f * nv + j]; label[f] = Int32(j) }
        }
        for _ in 0..<smoothIterations {
            var changed = false
            var next = label
            for f in 0..<m where best[f] > 0 {
                let nb = adj[f]
                var cands: [Int32] = [label[f]]
                for e in 0..<3 where nb[e] >= 0 { cands.append(label[Int(nb[e])]) }
                var bestCost = Float.infinity, pick = label[f]
                for c in cands where c >= 0 {
                    let s = scores[f * nv + Int(c)]
                    guard s > 0 else { continue }
                    var cost = -Foundation.log(s / best[f])
                    for e in 0..<3 where label[Int(max(nb[e], 0))] != c || nb[e] < 0 { cost += smoothWeight }
                    if cost < bestCost { bestCost = cost; pick = c }
                }
                if pick != label[f] { next[f] = pick; changed = true }
            }
            label = next
            if !changed { break }
        }
        return label
    }

    /// Per face its chart: connected runs (across shared edges) of faces with the same view.
    static func charts(_ label: [Int32], adj: [SIMD3<Int32>]) -> (chart: [Int32], count: Int) {
        var parent = Array(0..<label.count)
        func root(_ x: Int) -> Int {
            var x = x
            while parent[x] != x { parent[x] = parent[parent[x]]; x = parent[x] }
            return x
        }
        for f in 0..<label.count { for e in 0..<3 {
            let g = Int(adj[f][e])
            if g > f, label[g] == label[f] { let a = root(f), b = root(g); if a != b { parent[b] = a } }
        } }
        var id: [Int: Int32] = [:]
        var chart = [Int32](repeating: 0, count: label.count)
        for f in 0..<label.count {
            let r = root(f)
            if let c = id[r] { chart[f] = c } else { let c = Int32(id.count); id[r] = c; chart[f] = c }
        }
        return (chart, id.count)
    }

    // MARK: Seam levelling

    static func sampleMean(_ img: (pixels: [UInt8], width: Int, height: Int), _ uv: SIMD2<Float>) -> SIMD3<Float> {
        var acc = SIMD3<Float>(), n: Float = 0
        let cx = Int(uv.x.rounded()), cy = Int(uv.y.rounded())
        for dy in -1...1 { for dx in -1...1 {
            let x = min(img.width - 1, max(0, cx + dx)), y = min(img.height - 1, max(0, cy + dy))
            let i = (y * img.width + x) * 4
            acc += SIMD3(Float(img.pixels[i]), Float(img.pixels[i + 1]), Float(img.pixels[i + 2]))
            n += 1
        } }
        return acc / n
    }

    /// Solves the regularised least squares sum (g_a − g_b − d)^2 + reg sum g^2 for `n` unknowns (one channel).
    static func solveGains(_ eqs: [(Int, Int, Float)], n: Int) -> [Float] {
        guard !eqs.isEmpty, n > 0 else { return [Float](repeating: 0, count: n) }
        var a = [Double](repeating: 0, count: n * n), b = [Double](repeating: 0, count: n)
        for (i, j, d) in eqs where i != j {
            a[i * n + i] += 1; a[j * n + j] += 1; a[i * n + j] -= 1; a[j * n + i] -= 1
            b[i] += Double(d); b[j] -= Double(d)
        }
        let reg = Double(seamReg) * max(1, Double(eqs.count) / Double(n))
        for i in 0..<n { a[i * n + i] += reg }
        // Gaussian elimination (symmetric positive definite, small)
        for c in 0..<n {
            let p = a[c * n + c]
            guard abs(p) > 1e-12 else { continue }
            for r in (c + 1)..<max(c + 1, n) {
                let f = a[r * n + c] / p
                guard f != 0 else { continue }
                for k in c..<n { a[r * n + k] -= f * a[c * n + k] }
                b[r] -= f * b[c]
            }
        }
        var x = [Double](repeating: 0, count: n)
        for r in stride(from: n - 1, through: 0, by: -1) {
            var s = b[r]
            for k in (r + 1)..<max(r + 1, n) { s -= a[r * n + k] * x[k] }
            x[r] = abs(a[r * n + r]) > 1e-12 ? s / a[r * n + r] : 0
        }
        return x.map(Float.init)
    }

    /// A smooth field (w x h, RGB) through scattered values at tile points: push-pull on a grid of `step` pixels.
    static func spread(_ points: [SIMD2<Float>], _ values: [SIMD3<Float>], width w: Int, height h: Int, step: Int = 4) -> (field: [SIMD3<Float>], gw: Int, gh: Int) {
        let gw = max(1, (w + step - 1) / step), gh = max(1, (h + step - 1) / step)
        var acc = [SIMD3<Float>](repeating: .zero, count: gw * gh), wt = [Float](repeating: 0, count: gw * gh)
        for (p, val) in zip(points, values) {
            let x = min(gw - 1, max(0, Int(p.x / Float(step)))), y = min(gh - 1, max(0, Int(p.y / Float(step))))
            acc[y * gw + x] += val; wt[y * gw + x] += 1
        }
        var levels: [([SIMD3<Float>], [Float], Int, Int)] = [(acc, wt, gw, gh)]
        while max(levels.last!.2, levels.last!.3) > 1 {
            let (a, b, lw, lh) = levels.last!
            let nw = (lw + 1) / 2, nh = (lh + 1) / 2
            var na = [SIMD3<Float>](repeating: .zero, count: nw * nh), nb = [Float](repeating: 0, count: nw * nh)
            for y in 0..<lh { for x in 0..<lw {
                na[(y / 2) * nw + x / 2] += a[y * lw + x]; nb[(y / 2) * nw + x / 2] += b[y * lw + x]
            } }
            levels.append((na, nb, nw, nh))
        }
        var (a0, b0, _, _) = levels.last!
        var field = zip(a0, b0).map { $1 > 0 ? $0 / $1 : SIMD3<Float>() }
        var fw = levels.last!.2, fh = levels.last!.3
        for (a, b, lw, lh) in levels.dropLast().reversed() {
            var next = [SIMD3<Float>](repeating: .zero, count: lw * lh)
            for y in 0..<lh { for x in 0..<lw {
                let up = field[min(fh - 1, y / 2) * fw + min(fw - 1, x / 2)]
                let i = y * lw + x
                let k = min(1, b[i])
                let own = b[i] > 0 ? a[i] / b[i] : SIMD3<Float>()
                next[i] = k * own + (1 - k) * up
            } }
            // soften the nearest-neighbour upsampling
            var soft = next
            for y in 0..<lh { for x in 0..<lw {
                let l = next[y * lw + max(0, x - 1)], r = next[y * lw + min(lw - 1, x + 1)]
                let u = next[max(0, y - 1) * lw + x], d = next[min(lh - 1, y + 1) * lw + x]
                soft[y * lw + x] = 0.5 * next[y * lw + x] + 0.125 * (l + r + u + d)
            } }
            field = soft; fw = lw; fh = lh
        }
        a0 = []; b0 = []
        return (field, fw, fh)
    }

    /// Skyline packing on 4-pixel columns: each rectangle goes where the highest column under it is lowest (leftmost
    /// on ties). Returns the top-left positions and the height used.
    static func skyline(_ sizes: [SIMD2<Int>], width: Int, cell: Int = 4) -> ([SIMD2<Int>], Int) {
        let cols = max(1, width / cell)
        var height = [Int](repeating: 0, count: cols)
        var out: [SIMD2<Int>] = []
        out.reserveCapacity(sizes.count)
        var used = 0
        var deque = [Int](repeating: 0, count: cols)
        for sz in sizes {
            let wc = min(cols, (sz.x + cell - 1) / cell)
            // sliding-window maximum of `height` over windows of wc columns (monotonic deque)
            var head = 0, tail = 0
            var bestX = 0, bestY = Int.max
            for x in 0..<cols {
                while tail > head && height[deque[tail - 1]] <= height[x] { tail -= 1 }
                deque[tail] = x; tail += 1
                if deque[head] <= x - wc { head += 1 }
                if x >= wc - 1 {
                    let y = height[deque[head]]
                    if y < bestY { bestY = y; bestX = x - wc + 1 }
                }
            }
            for x in bestX..<(bestX + wc) { height[x] = bestY + sz.y }
            out.append(SIMD2(bestX * cell, bestY))
            used = max(used, bestY + sz.y)
        }
        return (out, used)
    }

    // MARK: All

    static func texture(positions v: [SIMD3<Float>], indices t: [UInt32], views: [ShellView], voxel: Float,
                        atlasSize: Int) -> Output? {
        var s: [String: Double] = [:]
        var t0 = Date()
        let m = t.count / 3, nv = views.count
        guard m > 0, nv > 0 else { return nil }
        let adj = adjacency(t)
        let sc = scores(v, t, views: views, voxel: voxel)
        s["visibility_s"] = Date().timeIntervalSince(t0); t0 = Date()
        let label = select(sc, faces: m, views: nv, adj: adj)
        let (chart, nc) = charts(label, adj: adj)
        var chartView = [Int32](repeating: -1, count: nc)
        for f in 0..<m { chartView[Int(chart[f])] = label[f] }
        s["select_s"] = Date().timeIntervalSince(t0); t0 = Date()

        // Corner pixels in their chart's view, chart rectangles.
        var cornerPx = [SIMD2<Float>](repeating: .zero, count: m * 3)
        var lo = [SIMD2<Float>](repeating: SIMD2(repeating: .infinity), count: nc), hi = [SIMD2<Float>](repeating: SIMD2(repeating: -.infinity), count: nc)
        for f in 0..<m where label[f] >= 0 {
            let view = views[Int(label[f])]
            for k in 0..<3 {
                let (uv, _) = view.cam.project(view.pose.toCamera(v[Int(t[3 * f + k])]))
                cornerPx[3 * f + k] = uv
                lo[Int(chart[f])] = simd_min(lo[Int(chart[f])], uv)
                hi[Int(chart[f])] = simd_max(hi[Int(chart[f])], uv)
            }
        }
        var src = [SIMD2<Float>](repeating: .zero, count: nc)
        var areaSum: Float = 0
        let pad = Float(tilePad)
        for c in 0..<nc where chartView[c] >= 0 {
            lo[c] = lo[c].rounded(.down) - 1; hi[c] = hi[c].rounded(.up) + 1
            src[c] = hi[c] - lo[c]
            areaSum += (src[c].x + 2 * pad) * (src[c].y + 2 * pad)
        }
        var scale = min(1, (0.85 * Float(atlasSize * atlasSize) / max(areaSum, 1)).squareRoot())
        var tile = [SIMD2<Int>](repeating: .zero, count: nc), tilePos = tile
        var unseenPos = SIMD2<Int>.zero
        while true {
            for c in 0..<nc where chartView[c] >= 0 {
                tile[c] = SIMD2(max(2 * tilePad + 1, Int((src[c].x * scale).rounded(.up)) + 2 * tilePad),
                                max(2 * tilePad + 1, Int((src[c].y * scale).rounded(.up)) + 2 * tilePad))
            }
            // skyline packing (tallest first, each at the lowest place it fits); the unseen tile last
            var order = (0..<nc).filter { chartView[$0] >= 0 }.sorted { tile[$0].y > tile[$1].y }
            order.append(-1)
            let sizes = order.map { $0 >= 0 ? tile[$0] : SIMD2(unseenTile, unseenTile) }
            let (pos, used) = skyline(sizes, width: atlasSize)
            for (c, p) in zip(order, pos) { if c >= 0 { tilePos[c] = p } else { unseenPos = p } }
            if used <= atlasSize { break }
            scale *= 0.95
        }
        s["pack_s"] = Date().timeIntervalSince(t0); t0 = Date()

        // Seam levelling: per-photo gains, then local corrections at the seam vertices.
        let used = Array(Set(chartView.filter { $0 >= 0 }.map(Int.init))).sorted()
        var thumbs: [Int: (pixels: [UInt8], width: Int, height: Int)] = [:]
        let thumbList = SharedBuffer([(pixels: [UInt8], width: Int, height: Int)?](repeating: nil, count: used.count))
        DispatchQueue.concurrentPerform(iterations: used.count) { i in thumbList.a[i] = thumbnail(views[used[i]].image, maxPixel: thumbWidth) }
        for (i, j) in used.enumerated() { thumbs[j] = thumbList.a[i] }
        var incid: [(vertex: Int, chart: Int)] = []
        do {
            var seen = Set<Int>()
            for f in 0..<m where label[f] >= 0 {
                for k in 0..<3 {
                    let key = Int(t[3 * f + k]) * nc + Int(chart[f])
                    if seen.insert(key).inserted { incid.append((Int(t[3 * f + k]), Int(chart[f]))) }
                }
            }
        }
        incid.sort { $0.vertex < $1.vertex }
        var seamVert: [Int] = [], seamChart: [Int] = [], seamCol: [SIMD3<Float>] = []
        var i0 = 0
        while i0 < incid.count {
            var i1 = i0
            while i1 < incid.count && incid[i1].vertex == incid[i0].vertex { i1 += 1 }
            if i1 - i0 >= 2 {
                for i in i0..<i1 {
                    let c = incid[i].chart, j = Int(chartView[c])
                    guard let th = thumbs[j] else { continue }
                    let cam = views[j].cam.scaled(to: th.width, th.height)
                    let (uv, _) = cam.project(views[j].pose.toCamera(v[incid[i].vertex]))
                    seamVert.append(incid[i].vertex); seamChart.append(c); seamCol.append(sampleMean(th, uv))
                }
            }
            i0 = i1
        }
        // per-view gains from pairs at the same vertex
        var eqs: [[(Int, Int, Float)]] = [[], [], []]
        var k0 = 0
        while k0 < seamVert.count {
            var k1 = k0
            while k1 < seamVert.count && seamVert[k1] == seamVert[k0] { k1 += 1 }
            for k in (k0 + 1)..<max(k0 + 1, k1) {
                let a = seamCol[k0], b = seamCol[k]
                guard simd_reduce_min(a) > 12, simd_reduce_max(a) < 245, simd_reduce_min(b) > 12, simd_reduce_max(b) < 245 else { continue }
                let va = Int(chartView[seamChart[k0]]), vb = Int(chartView[seamChart[k]])
                guard va != vb else { continue }
                for ch in 0..<3 { eqs[ch].append((va, vb, Foundation.log(b[ch]) - Foundation.log(a[ch]))) }
            }
            k0 = k1
        }
        var viewGain = [SIMD3<Float>](repeating: SIMD3(repeating: 1), count: nv)
        for ch in 0..<3 {
            let g = solveGains(eqs[ch], n: nv)
            for j in 0..<nv { viewGain[j][ch] = min(gainLimits.upperBound, max(gainLimits.lowerBound, exp(g[j]))) }
        }
        // local corrections: each chart's colour at a seam vertex moves to the mean of all charts there
        var corr = [SIMD3<Float>](repeating: .zero, count: seamVert.count)
        k0 = 0
        while k0 < seamVert.count {
            var k1 = k0
            while k1 < seamVert.count && seamVert[k1] == seamVert[k0] { k1 += 1 }
            var mean = SIMD3<Float>()
            for k in k0..<k1 { mean += seamCol[k] * viewGain[Int(chartView[seamChart[k]])] }
            mean /= Float(k1 - k0)
            for k in k0..<k1 {
                let c = mean - seamCol[k] * viewGain[Int(chartView[seamChart[k]])]
                corr[k] = simd_clamp(c, SIMD3(repeating: -localLimit), SIMD3(repeating: localLimit))
            }
            k0 = k1
        }
        var byChart = [[Int]](repeating: [], count: nc)
        for (k, c) in seamChart.enumerated() { byChart[c].append(k) }
        thumbs.removeAll()
        s["seams_s"] = Date().timeIntervalSince(t0); t0 = Date()

        // The atlas: each chart's rectangle of its photo, scaled into its tile, then levelled in place.
        let bytesPerRow = atlasSize * 4
        guard let ctx = CGContext(data: nil, width: atlasSize, height: atlasSize, bitsPerComponent: 8, bytesPerRow: bytesPerRow,
                                  space: CGColorSpace(name: CGColorSpace.sRGB)!,
                                  bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue) else { return nil }
        ctx.interpolationQuality = .medium
        ctx.setFillColor(CGColor(srgbRed: 118 / 255, green: 118 / 255, blue: 122 / 255, alpha: 1))
        let A = CGFloat(atlasSize)
        ctx.fill(CGRect(x: CGFloat(unseenPos.x), y: A - CGFloat(unseenPos.y + unseenTile), width: CGFloat(unseenTile), height: CGFloat(unseenTile)))
        var kScale = [SIMD2<Float>](repeating: .zero, count: nc)
        var s0 = [SIMD2<Float>](repeating: .zero, count: nc)
        var need = [Float](repeating: 0, count: nv)          // per view: the largest atlas/source scale it is drawn at
        for c in 0..<nc where chartView[c] >= 0 {
            let k = SIMD2(Float(tile[c].x - 2 * tilePad), Float(tile[c].y - 2 * tilePad)) / simd_max(src[c], SIMD2(repeating: 1e-6))
            kScale[c] = k
            s0[c] = lo[c] - pad / k
            need[Int(chartView[c])] = max(need[Int(chartView[c])], simd_reduce_max(k))
        }
        // Photos are decoded two at a time, each only as large as its largest tile needs (JPEG decodes 1/2, 1/4 size
        // much faster), then drawn one by one.
        for batch in stride(from: 0, to: used.count, by: 2).map({ Array(used[$0..<min(used.count, $0 + 2)]) }) {
            let decoded = SharedBuffer([CGImage?](repeating: nil, count: batch.count))
            DispatchQueue.concurrentPerform(iterations: batch.count) { b in
                let view = views[batch[b]]
                let full = max(view.cam.width, view.cam.height)
                let px = Int((Float(full) * min(1, need[batch[b]] * 1.1)).rounded(.up))
                decoded.a[b] = px >= full * 9 / 10 ? fullImage(view.image) : scaledImage(view.image, maxPixel: px)
            }
            for (b, j) in batch.enumerated() {
                guard let img = decoded.a[b] else { continue }
                let r = Float(img.width) / Float(views[j].cam.width)           // decoded px per source px
                let iw = Float(img.width), ih = Float(img.height)
                for c in 0..<nc where Int(chartView[c]) == j {
                    let k = kScale[c]
                    let s1 = s0[c] + SIMD2(Float(tile[c].x), Float(tile[c].y)) / k
                    let c0 = simd_max(s0[c] * r, .zero).rounded(.down), c1 = simd_min(s1 * r, SIMD2(iw, ih)).rounded(.up)
                    guard c1.x - c0.x >= 1, c1.y - c0.y >= 1,
                          let crop = img.cropping(to: CGRect(x: CGFloat(c0.x), y: CGFloat(c0.y), width: CGFloat(c1.x - c0.x),
                                                             height: CGFloat(c1.y - c0.y))) else { continue }
                    // where the crop lands, in atlas pixels from the top-left
                    let d0 = SIMD2(Float(tilePos[c].x), Float(tilePos[c].y)) + (c0 / r - s0[c]) * k
                    let dsz = SIMD2(Float(crop.width), Float(crop.height)) / r * k
                    let tileRect = CGRect(x: CGFloat(tilePos[c].x), y: A - CGFloat(tilePos[c].y + tile[c].y),
                                          width: CGFloat(tile[c].x), height: CGFloat(tile[c].y))
                    ctx.saveGState()
                    ctx.clip(to: tileRect)
                    ctx.draw(crop, in: CGRect(x: CGFloat(d0.x), y: A - CGFloat(d0.y + dsz.y), width: CGFloat(dsz.x), height: CGFloat(dsz.y)))
                    ctx.restoreGState()
                }
            }
        }
        s["atlas_draw_s"] = Date().timeIntervalSince(t0); t0 = Date()
        guard let base = ctx.data?.assumingMemoryBound(to: UInt8.self) else { return nil }
        let pixels = UnsafeMutablePointer<UInt8>(base)
        nonisolated(unsafe) let px = pixels
        let charts = (0..<nc).filter { chartView[$0] >= 0 }
        DispatchQueue.concurrentPerform(iterations: charts.count) { ci in
            let c = charts[ci]
            let j = Int(chartView[c])
            let gain = viewGain[j]
            let tw = tile[c].x, th = tile[c].y
            var pts: [SIMD2<Float>] = [], vals: [SIMD3<Float>] = []
            for k in byChart[c] {
                let (uv, _) = views[j].cam.project(views[j].pose.toCamera(v[seamVert[k]]))
                pts.append((uv - s0[c]) * kScale[c]); vals.append(corr[k])
            }
            let step = 4
            let fieldInfo = pts.isEmpty ? nil : spread(pts, vals, width: tw, height: th, step: step)
            for y in 0..<th {
                let ay = tilePos[c].y + y
                guard ay < atlasSize else { break }
                for x in 0..<tw {
                    let ax = tilePos[c].x + x
                    guard ax < atlasSize else { break }
                    let i = ay * bytesPerRow + ax * 4
                    var col = SIMD3(Float(px[i]), Float(px[i + 1]), Float(px[i + 2])) * gain
                    if let (field, gw, gh) = fieldInfo {
                        // bilinear in the grid (cell centres at step * (i + 0.5))
                        let gx = (Float(x) + 0.5) / Float(step) - 0.5, gy = (Float(y) + 0.5) / Float(step) - 0.5
                        let x0 = min(gw - 1, max(0, Int(gx.rounded(.down)))), y0 = min(gh - 1, max(0, Int(gy.rounded(.down))))
                        let x1 = min(gw - 1, x0 + 1), y1 = min(gh - 1, y0 + 1)
                        let fx = min(1, max(0, gx - Float(x0))), fy = min(1, max(0, gy - Float(y0)))
                        let top = field[y0 * gw + x0] * (1 - fx) + field[y0 * gw + x1] * fx
                        let bot = field[y1 * gw + x0] * (1 - fx) + field[y1 * gw + x1] * fx
                        col += top * (1 - fy) + bot * fy
                    }
                    let o = simd_clamp(col, .zero, SIMD3(repeating: 255))
                    px[i] = UInt8(o.x); px[i + 1] = UInt8(o.y); px[i + 2] = UInt8(o.z)
                }
            }
        }
        guard let atlas = ctx.makeImage() else { return nil }
        s["atlas_level_s"] = Date().timeIntervalSince(t0); t0 = Date()

        // Vertices: one per (mesh vertex, chart); unseen faces share the grey tile.
        var key: [Int: UInt32] = [:]
        var outP: [SIMD3<Float>] = [], outUV: [SIMD2<Float>] = [], outI: [UInt32] = []
        outI.reserveCapacity(t.count)
        let unseenUV = (SIMD2(Float(unseenPos.x), Float(unseenPos.y)) + Float(unseenTile) / 2) / Float(atlasSize)
        for f in 0..<m {
            let c = label[f] >= 0 ? Int(chart[f]) : nc
            for k in 0..<3 {
                let vi = Int(t[3 * f + k])
                let kk = vi * (nc + 1) + c
                if let idx = key[kk] { outI.append(idx); continue }
                let uv: SIMD2<Float>
                if c < nc {
                    let a = (cornerPx[3 * f + k] - lo[c]) * kScale[c] + SIMD2(Float(tilePos[c].x), Float(tilePos[c].y)) + pad
                    uv = a / Float(atlasSize)
                } else {
                    uv = unseenUV
                }
                key[kk] = UInt32(outP.count)
                outP.append(v[vi]); outUV.append(uv); outI.append(UInt32(outP.count - 1))
            }
        }
        s["vertices_s"] = Date().timeIntervalSince(t0)
        s["faces"] = Double(m); s["charts"] = Double(charts.count); s["views"] = Double(nv)
        s["views_used"] = Double(used.count); s["stills_used"] = Double(used.filter { views[$0].isStill }.count)
        s["faces_unseen"] = Double(label.filter { $0 < 0 }.count); s["source_scale"] = Double(scale)
        return Output(mesh: TexturedMesh(positions: outP, uvs: outUV, indices: outI), atlas: atlas, stats: s)
    }

    static func jpeg(_ image: CGImage, quality: Double = 0.85) -> Data? {
        let data = NSMutableData()
        guard let dest = CGImageDestinationCreateWithData(data, UTType.jpeg.identifier as CFString, 1, nil) else { return nil }
        CGImageDestinationAddImage(dest, image, [kCGImageDestinationLossyCompressionQuality: quality] as CFDictionary)
        return CGImageDestinationFinalize(dest) ? data as Data : nil
    }
}
