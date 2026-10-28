import Foundation

/// One camera frame reduced to a grid of cells, which is all the phone checks need from it. Per cell: the brightest
/// pixel's brightness (0 to 1, the strongest of its colour channels, so a pure red or blue LED is as bright as a white
/// one; a small LED must not be averaged away by the dark around it) and the average colour of the bright pixels. `mean` is the whole frame's average brightness, `iso` and `exposureS` what the camera used.
struct CameraFrame: Equatable, Sendable {
    var t: Double
    var luma: [Float]
    var rgb: [UInt8]
    var mean: Float
    var iso: Float
    var exposureS: Double
}

enum CellGrid {
    static let cols = 32
    static let rows = 24

    /// Reduces a BGRA pixel buffer. Every `step`-th pixel in each direction is read (step 2 still hits a 3-pixel LED).
    static func reduce(base: UnsafePointer<UInt8>, width: Int, height: Int, bytesPerRow: Int, step: Int = 2,
                       cols: Int = CellGrid.cols, rows: Int = CellGrid.rows) -> (luma: [Float], rgb: [UInt8], mean: Float) {
        var luma = [Float](repeating: 0, count: cols * rows)
        var rgb = [UInt8](repeating: 0, count: cols * rows * 3)
        var total = 0
        var seen = 0
        for cy in 0..<rows {
            let y0 = cy * height / rows
            let y1 = (cy + 1) * height / rows
            for cx in 0..<cols {
                let x0 = cx * width / cols
                let x1 = (cx + 1) * width / cols
                var maxL = 0
                var maxR = 0, maxG = 0, maxB = 0
                var brightR = 0, brightG = 0, brightB = 0, bright = 0
                for y in stride(from: y0, to: y1, by: step) {
                    let row = base + y * bytesPerRow
                    for x in stride(from: x0, to: x1, by: step) {
                        let p = row + x * 4
                        let b = Int(p[0]), g = Int(p[1]), r = Int(p[2])
                        let v = max(r, max(g, b))
                        total += (r + 2 * g + b) >> 2   // the frame's average is perceived brightness, for the exposure checks
                        seen += 1
                        if v > maxL { maxL = v; maxR = r; maxG = g; maxB = b }
                        if v >= 128 { brightR += r; brightG += g; brightB += b; bright += 1 }
                    }
                }
                let cell = cy * cols + cx
                luma[cell] = Float(maxL) / 255
                if bright > 0 {
                    rgb[cell * 3] = UInt8(brightR / bright)
                    rgb[cell * 3 + 1] = UInt8(brightG / bright)
                    rgb[cell * 3 + 2] = UInt8(brightB / bright)
                } else {
                    rgb[cell * 3] = UInt8(maxR)
                    rgb[cell * 3 + 1] = UInt8(maxG)
                    rgb[cell * 3 + 2] = UInt8(maxB)
                }
            }
        }
        return (luma, rgb, seen > 0 ? Float(total) / Float(seen) / 255 : 0)
    }
}

/// Finds the small bright region that changes, which is the LED the person is pointing at.
enum LedTracker {
    struct Track: Equatable, Sendable {
        var cell: Int
        /// The cell's brightness over time (the brightest of it and its neighbours, so a hand that drifts one cell
        /// does not lose the LED).
        var series: [BrightnessSample]
        /// Biggest minus smallest brightness the cell showed.
        var range: Float
        var mean: Float
    }

    /// The cell whose brightness changes most (among the equally changing ones, the brightest), or nil when nothing
    /// changed by `minRange`.
    static func track(_ frames: [CameraFrame], cols: Int = CellGrid.cols, rows: Int = CellGrid.rows, minRange: Float = 0.06) -> Track? {
        guard frames.count >= 2 else { return nil }
        let n = cols * rows
        var lo = [Float](repeating: 1, count: n)
        var hi = [Float](repeating: 0, count: n)
        for f in frames where f.luma.count == n {
            for i in 0..<n {
                let v = f.luma[i]
                if v < lo[i] { lo[i] = v }
                if v > hi[i] { hi[i] = v }
            }
        }
        var bestRange: Float = 0
        for i in 0..<n { bestRange = max(bestRange, hi[i] - lo[i]) }
        guard bestRange >= minRange else { return nil }
        var cell = 0
        var cellMax: Float = -1
        for i in 0..<n where hi[i] - lo[i] >= 0.9 * bestRange && hi[i] > cellMax {
            cell = i
            cellMax = hi[i]
        }
        return Track(cell: cell, series: series(frames, cell: cell, cols: cols, rows: rows), range: hi[cell] - lo[cell],
                     mean: mean(frames, cell: cell, cols: cols, rows: rows))
    }

    /// The brightest cell over the whole timeline, for an LED that does not change.
    static func brightest(_ frames: [CameraFrame], cols: Int = CellGrid.cols, rows: Int = CellGrid.rows) -> Track? {
        guard !frames.isEmpty else { return nil }
        let n = cols * rows
        var sum = [Float](repeating: 0, count: n)
        var lo = [Float](repeating: 1, count: n)
        var hi = [Float](repeating: 0, count: n)
        var used: Float = 0
        for f in frames where f.luma.count == n {
            used += 1
            for i in 0..<n {
                sum[i] += f.luma[i]
                lo[i] = min(lo[i], f.luma[i])
                hi[i] = max(hi[i], f.luma[i])
            }
        }
        guard used > 0 else { return nil }
        var cell = 0
        for i in 0..<n where sum[i] > sum[cell] { cell = i }
        return Track(cell: cell, series: series(frames, cell: cell, cols: cols, rows: rows), range: hi[cell] - lo[cell],
                     mean: sum[cell] / used)
    }

    static func series(_ frames: [CameraFrame], cell: Int, cols: Int, rows: Int) -> [BrightnessSample] {
        let cx = cell % cols, cy = cell / cols
        let n = cols * rows
        return frames.compactMap { f in
            guard f.luma.count == n else { return nil }
            var best: Float = 0
            for y in max(0, cy - 1)...min(rows - 1, cy + 1) {
                for x in max(0, cx - 1)...min(cols - 1, cx + 1) { best = max(best, f.luma[y * cols + x]) }
            }
            return BrightnessSample(t: f.t, v: best)
        }
    }

    private static func mean(_ frames: [CameraFrame], cell: Int, cols: Int, rows: Int) -> Float {
        let values = frames.filter { $0.luma.count == cols * rows }.map { $0.luma[cell] }
        return values.isEmpty ? 0 : values.reduce(0, +) / Float(values.count)
    }

    /// Where the tracked cell sits in the picture, 0 to 1 from the top left, for the outline drawn over the preview.
    static func centre(of cell: Int, cols: Int = CellGrid.cols, rows: Int = CellGrid.rows) -> (x: Double, y: Double) {
        (x: (Double(cell % cols) + 0.5) / Double(cols), y: (Double(cell / cols) + 0.5) / Double(rows))
    }

    /// The average colour of the cell in the frames where `on` says the LED was lit.
    static func colour(of cell: Int, frames: [CameraFrame], on: [LedSample]) -> (r: Double, g: Double, b: Double)? {
        var r = 0.0, g = 0.0, b = 0.0, n = 0.0
        let lit = Set(on.filter(\.on).map(\.t))
        for f in frames where lit.contains(f.t) && f.rgb.count > cell * 3 + 2 {
            r += Double(f.rgb[cell * 3]); g += Double(f.rgb[cell * 3 + 1]); b += Double(f.rgb[cell * 3 + 2])
            n += 1
        }
        return n > 0 ? (r / n, g / n, b / n) : nil
    }
}

/// A colour word for an LED's average colour.
enum HueName {
    /// red, orange, yellow, green, cyan, blue, purple, pink, or white when there is almost no colour.
    static func name(r: Double, g: Double, b: Double) -> String {
        let hi = max(r, g, b), lo = min(r, g, b)
        guard hi > 0 else { return "white" }
        let saturation = (hi - lo) / hi
        if saturation < 0.2 { return "white" }
        var hue: Double
        let d = hi - lo
        if hi == r { hue = (g - b) / d } else if hi == g { hue = 2 + (b - r) / d } else { hue = 4 + (r - g) / d }
        hue *= 60
        if hue < 0 { hue += 360 }
        switch hue {
        case 345..., ..<15: return "red"
        case 15..<40: return "orange"
        case 40..<70: return "yellow"
        case 70..<165: return "green"
        case 165..<200: return "cyan"
        case 200..<260: return "blue"
        case 260..<300: return "purple"
        default: return "pink"
        }
    }
}

enum LedObservation {
    struct Result: Equatable, Sendable {
        var state: ValueState
        var color: String?
        var cell: Int
    }

    /// What the camera saw the LED do over `frames` (a few seconds): blinking when the tracked cell switched on and off
    /// at least twice, otherwise lit or dark. A steady LED is "lit" when the brightest cell is bright and stands out
    /// from the middle of the picture by `standout`; a dim glow in a bright room reads dark. This is a judgement of
    /// the picture, not a measurement, and the sheet says so.
    static func observe(_ frames: [CameraFrame], cols: Int = CellGrid.cols, rows: Int = CellGrid.rows, standout: Float = 0.3,
                        litLevel: Float = 0.7) -> Result? {
        if let t = LedTracker.track(frames, cols: cols, rows: rows), let onOff = LedThreshold.onOff(t.series),
           LedThreshold.edges(onOff) >= 2 {
            let c = LedTracker.colour(of: t.cell, frames: frames, on: onOff)
            return Result(state: .blinking, color: c.map { HueName.name(r: $0.r, g: $0.g, b: $0.b) }, cell: t.cell)
        }
        guard let b = LedTracker.brightest(frames, cols: cols, rows: rows) else { return nil }
        let last = frames.last { $0.luma.count == cols * rows }
        let typical = last.map { median($0.luma) } ?? 0
        let lit = b.mean >= litLevel && b.mean - typical >= standout
        var colour: String?
        if lit, let f = last, f.rgb.count > b.cell * 3 + 2 {
            colour = HueName.name(r: Double(f.rgb[b.cell * 3]), g: Double(f.rgb[b.cell * 3 + 1]), b: Double(f.rgb[b.cell * 3 + 2]))
        }
        return Result(state: lit ? .on : .off, color: colour, cell: b.cell)
    }

    private static func median(_ v: [Float]) -> Float {
        let s = v.sorted()
        return s.isEmpty ? 0 : s[s.count / 2]
    }
}

/// The blink decoder fed from camera frames: the tracker finds the LED, the threshold turns its brightness into on and
/// off, the decoder reads the code.
enum BlinkAnalysis {
    struct Result: Equatable, Sendable {
        var progress: BlinkDecoder.Progress
        /// The LED's place in the picture (0 to 1 from the top left), when one was found.
        var marker: (x: Double, y: Double)?

        static func == (a: Result, b: Result) -> Bool {
            a.progress == b.progress && a.marker?.x == b.marker?.x && a.marker?.y == b.marker?.y
        }
    }

    static func run(_ frames: [CameraFrame], code: BlinkCode) -> Result {
        guard let t = LedTracker.track(frames) else { return Result(progress: .searching, marker: nil) }
        let centre = LedTracker.centre(of: t.cell)
        guard let onOff = LedThreshold.onOff(t.series) else { return Result(progress: .searching, marker: centre) }
        return Result(progress: BlinkDecoder.decode(onOff, code: code), marker: centre)
    }
}
