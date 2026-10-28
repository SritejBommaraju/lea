import Foundation
import Testing
@testable import RepairOS

/// The status-LED blink code, decoded from synthetic timelines: clean, jittered, with missed frames, still running,
/// and not a code. The camera cannot run in the simulator, so the frames here are built by hand; everything from the
/// frame grid onwards (tracker, threshold, decoder) is the real code.
enum BlinkSynth {
    static let code = BlinkCode(flashOnMS: 100, flashOffMS: 100, longOnMS: 600, longOffMS: 300, gapMS: 1000,
                                preambleFlashes: 3, endFlashes: 3, longCounts: (1, 2, 3))

    typealias Segment = (on: Bool, ms: Double)

    /// The code for these results, each given as its number of long blinks, with dark time before and after.
    static func segments(longCounts: [Int], lead: Double = 400, tail: Double = 1600, preamble: Bool = true, end: Bool = true) -> [Segment] {
        var s: [Segment] = [(false, lead)]
        if preamble {
            for _ in 0..<3 { s += [(true, 100), (false, 100)] }
            s.append((false, 1000))
        }
        for n in longCounts {
            for _ in 0..<n { s += [(true, 600), (false, 300)] }
            s.append((false, 1000))
        }
        if end { for _ in 0..<3 { s += [(true, 100), (false, 100)] } }
        s.append((false, tail))
        return s
    }

    /// Samples at `fps`; `jitterMS` moves each sample time by up to that much (deterministically), `drop` removes frames.
    static func samples(_ segments: [Segment], fps: Double = 60, jitterMS: Double = 0, drop: Set<Int> = [],
                        cutAtMS: Double? = nil) -> [LedSample] {
        var edges: [(end: Double, on: Bool)] = []
        var t = 0.0
        for seg in segments { t += seg.ms; edges.append((t, seg.on)) }
        let total = cutAtMS ?? t
        var out: [LedSample] = []
        var seed: UInt64 = 12345
        var index = 0
        var time = 0.0
        while time < total {
            defer { index += 1; time += 1000 / fps }
            if drop.contains(index) { continue }
            seed = seed &* 6364136223846793005 &+ 1442695040888963407
            let unit = Double(seed >> 33) / Double(1 << 31)          // 0 ..< 1
            let at = max(0, time + (unit * 2 - 1) * jitterMS)
            let on = edges.first { at < $0.end }?.on ?? false
            out.append(LedSample(t: at / 1000, on: on))
        }
        return out
    }

    /// Camera frames of a 32 x 24 grid in which one cell follows `samples` (on or off), the rest is dark, and any
    /// `steady` cells stay at a fixed brightness (a window, a screen).
    static func frames(_ samples: [LedSample], cell: Int, on: Float = 0.9, off: Float = 0.1, background: Float = 0.1,
                       steady: [Int: Float] = [:], flicker: [Int: Float] = [:], rgb: (UInt8, UInt8, UInt8) = (0, 255, 0)) -> [CameraFrame] {
        let n = CellGrid.cols * CellGrid.rows
        return samples.enumerated().map { i, s in
            var luma = [Float](repeating: background, count: n)
            for (c, v) in steady { luma[c] = v }
            for (c, amplitude) in flicker { luma[c] = background + (i % 2 == 0 ? amplitude : 0) }
            luma[cell] = s.on ? on : off
            var colours = [UInt8](repeating: 0, count: n * 3)
            colours[cell * 3] = rgb.0; colours[cell * 3 + 1] = rgb.1; colours[cell * 3 + 2] = rgb.2
            return CameraFrame(t: s.t, luma: luma, rgb: colours, mean: background, iso: 100, exposureS: 1.0 / 60)
        }
    }
}

struct BlinkDecoderTests {
    let code = BlinkSynth.code

    @Test func readsACleanCode() {
        let samples = BlinkSynth.samples(BlinkSynth.segments(longCounts: [1, 2, 3, 1]))
        #expect(BlinkDecoder.decode(samples, code: code) == .complete([.ok, .fail, .unknown, .ok]))
    }

    @Test func readsACodeWithJitterOnEveryFrameTime() {
        let samples = BlinkSynth.samples(BlinkSynth.segments(longCounts: [1, 1, 2]), jitterMS: 4)
        #expect(BlinkDecoder.decode(samples, code: code) == .complete([.ok, .ok, .fail]))
    }

    @Test func readsACodeWithMissedFrames() {
        // Frames lost at an edge, in the middle of a flash and in the middle of a long blink.
        let clean = BlinkSynth.samples(BlinkSynth.segments(longCounts: [2, 1]))
        let lost = Set([26, 27, 33, 80, 81, 95, 120])
        let samples = BlinkSynth.samples(BlinkSynth.segments(longCounts: [2, 1]), drop: lost)
        #expect(samples.count == clean.count - lost.count)
        #expect(BlinkDecoder.decode(samples, code: code) == .complete([.fail, .ok]))
    }

    @Test func readsACodeWhenTheCaptureStartsLateInTheDark() {
        let samples = BlinkSynth.samples(BlinkSynth.segments(longCounts: [3], lead: 2500))
        #expect(BlinkDecoder.decode(samples, code: code) == .complete([.unknown]))
    }

    @Test func reportsHowFarItHasGotWhileTheCodeIsStillRunning() {
        // 400 ms dark, the preamble to 1000, a gap to 2000, check 1 to 2900, check 2 (two blinks) to 5700, a gap to 6700:
        // the third check's first blink starts at 6700, and the capture stops in the middle of it.
        let segments = BlinkSynth.segments(longCounts: [1, 2, 3], end: false)
        let cut = 7000.0
        let partial = BlinkSynth.samples(segments, cutAtMS: cut)
        #expect(BlinkDecoder.decode(partial, code: code) == .reading([.ok, .fail]))
        // Only the preamble so far
        let start = BlinkSynth.samples(BlinkSynth.segments(longCounts: [1], end: false), cutAtMS: 400 + 600 + 1000 + 500)
        #expect(BlinkDecoder.decode(start, code: code) == .reading([]))
    }

    @Test func nothingBlinkingIsNotACode() {
        let dark = (0..<300).map { LedSample(t: Double($0) / 60, on: false) }
        #expect(BlinkDecoder.decode(dark, code: code) == .searching)
        #expect(BlinkDecoder.decode([], code: code) == .searching)
    }

    @Test func aCodeCaughtWithoutItsPreambleIsNotRead() {
        let samples = BlinkSynth.samples(BlinkSynth.segments(longCounts: [1, 2], preamble: false))
        let got = BlinkDecoder.decode(samples, code: code)
        #expect(got != .complete([.ok, .fail]))
    }

    @Test func aRepeatingCodeIsReadFromTheSecondRoundWhenTheFirstWasCutOff() {
        // The tail of one round (checks and end flashes), then a whole round.
        let cutRound = BlinkSynth.segments(longCounts: [2], preamble: false, tail: 1000)
        let whole = BlinkSynth.segments(longCounts: [1, 3], lead: 0)
        let samples = BlinkSynth.samples(cutRound + whole)
        #expect(BlinkDecoder.decode(samples, code: code) == .complete([.ok, .unknown]))
    }

    @Test func aBlinkCountThatIsNotAResultIsUnreadableAndSaysWhere() {
        let samples = BlinkSynth.samples(BlinkSynth.segments(longCounts: [1, 4]))
        guard case .unreadable(let why) = BlinkDecoder.decode(samples, code: code) else {
            Issue.record("expected unreadable")
            return
        }
        #expect(why.contains("Check 2") && why.contains("4 times"))
    }

    @Test func aFlashOfTheWrongLengthIsUnreadable() {
        var s = BlinkSynth.segments(longCounts: [1])
        s[1] = (true, 250)   // the first preamble flash is neither short nor long
        let samples = BlinkSynth.samples(s)
        #expect(BlinkDecoder.decode(samples, code: code) != .complete([.ok]))
    }

    @Test func theEndFlashesMayFollowTheLastBlinkWithoutALongGap() {
        var s: [BlinkSynth.Segment] = [(false, 400)]
        for _ in 0..<3 { s += [(true, 100), (false, 100)] }
        s.append((false, 1000))
        s += [(true, 600), (false, 300), (true, 600), (false, 300)]       // a fail, then straight into the end flashes
        for _ in 0..<3 { s += [(true, 100), (false, 100)] }
        s.append((false, 1600))
        #expect(BlinkDecoder.decode(BlinkSynth.samples(s), code: code) == .complete([.fail]))
    }

    @Test func thePlanSetsTheTimingsAndTheCountsOfTheCode() throws {
        let plan = try #require(try ReportModelTests.example("report.design_engine_fix_plan.json").selfTest)
        let fromPlan = try #require(BlinkCode(plan: try #require(plan.blink)))
        #expect(fromPlan == BlinkSynth.code)
        #expect(fromPlan.outcome(forLongCount: 1) == .ok && fromPlan.outcome(forLongCount: 2) == .fail)
        #expect(fromPlan.outcome(forLongCount: 3) == .unknown && fromPlan.outcome(forLongCount: 4) == nil)
        #expect(abs(fromPlan.boundaryMS - 510) < 1e-9)
    }

    @Test func aPlanWhoseResultsShareACountIsNotRead() throws {
        let plan = try #require(try ReportModelTests.example("report.design_engine_fix_plan.json").selfTest)
        let blink = try #require(plan.blink)
        #expect(BlinkCode(plan: Blink(longBlinks: [1, 1, 3], timings: blink.timings)) == nil)
        #expect(BlinkCode(plan: Blink(longBlinks: [1, 2], timings: blink.timings)) == nil)
        #expect(BlinkCode(plan: Blink(longBlinks: [0, 2, 3], timings: blink.timings)) == nil)
        let other = try #require(BlinkCode(plan: Blink(longBlinks: [2, 3, 4], timings: blink.timings)))
        #expect(other.outcome(forLongCount: 2) == .ok && other.outcome(forLongCount: 4) == .unknown && other.outcome(forLongCount: 1) == nil)
    }

    // MARK: Edges

    @Test func edgesSitHalfwayBetweenTheFramesThatSawThem() {
        let s = [LedSample(t: 0.0, on: false), LedSample(t: 0.1, on: true), LedSample(t: 0.2, on: true), LedSample(t: 0.3, on: false)]
        let got = BlinkDecoder.pulses(s)
        #expect(got.pulses.count == 1 && abs(got.pulses[0].startMS - 50) < 1e-6 && abs(got.pulses[0].endMS - 250) < 1e-6)
        #expect(abs((got.tailDarkMS ?? 0) - 50) < 1e-6)
    }

    @Test func aPulseAlreadyOnAtTheFirstFrameIsDropped() {
        let s = [LedSample(t: 0.0, on: true), LedSample(t: 0.1, on: false), LedSample(t: 0.2, on: false)]
        #expect(BlinkDecoder.pulses(s).pulses.isEmpty)
    }
}

/// From camera frames: the tracker finds the LED among other bright things, the threshold and decoder read it.
struct LedFromFramesTests {
    let cell = 7 * CellGrid.cols + 10

    @Test func readsTheCodeFromTheCellThatChanges() {
        let samples = BlinkSynth.samples(BlinkSynth.segments(longCounts: [1, 2]))
        // a bright window that never changes, and a screen that flickers a little
        let frames = BlinkSynth.frames(samples, cell: cell, steady: [100: 0.95, 101: 0.95], flicker: [400: 0.1])
        let got = BlinkAnalysis.run(frames, code: BlinkSynth.code)
        #expect(got.progress == .complete([.ok, .fail]))
        let centre = LedTracker.centre(of: cell)
        #expect(got.marker?.x == centre.x && got.marker?.y == centre.y)
    }

    @Test func aLedThatDriftsOneCellIsStillFollowed() {
        let samples = BlinkSynth.samples(BlinkSynth.segments(longCounts: [3]))
        let n = CellGrid.cols * CellGrid.rows
        let frames: [CameraFrame] = samples.enumerated().map { i, s in
            var luma = [Float](repeating: 0.1, count: n)
            luma[s.on ? (i < 40 ? cell : cell + 1) : cell] = s.on ? 0.9 : 0.1
            return CameraFrame(t: s.t, luma: luma, rgb: [UInt8](repeating: 0, count: n * 3), mean: 0.1, iso: 100, exposureS: 0.01)
        }
        #expect(BlinkAnalysis.run(frames, code: BlinkSynth.code).progress == .complete([.unknown]))
    }

    @Test func aPictureWithNothingChangingFindsNoLed() {
        let samples = (0..<200).map { LedSample(t: Double($0) / 60, on: false) }
        let frames = BlinkSynth.frames(samples, cell: cell, steady: [5: 0.9])
        let got = BlinkAnalysis.run(frames, code: BlinkSynth.code)
        #expect(got.progress == .searching && got.marker == nil)
    }

    @Test func theThresholdFollowsTheSeriesItselfAndIgnoresNoise() throws {
        var seed: UInt64 = 99
        let series: [BrightnessSample] = (0..<240).map { i in
            seed = seed &* 6364136223846793005 &+ 1442695040888963407
            let noise = Float(seed >> 40) / Float(1 << 24) * 0.06 - 0.03
            return BrightnessSample(t: Double(i) / 60, v: ((i / 30) % 2 == 0 ? 0.15 : 0.7) + noise)
        }
        let got = try #require(LedThreshold.onOff(series))
        #expect(LedThreshold.edges(got) == 7)
        #expect(got[0].on == false && got[45].on == true)
        let flat = (0..<100).map { BrightnessSample(t: Double($0) / 60, v: 0.5 + Float($0 % 2) * 0.01) }
        #expect(LedThreshold.onOff(flat) == nil)
    }

    // MARK: What an LED is doing

    @Test func aSteadyBrightLedIsLitWithItsColour() throws {
        let samples = (0..<180).map { LedSample(t: Double($0) / 60, on: true) }
        let frames = BlinkSynth.frames(samples, cell: cell, on: 0.95, rgb: (255, 30, 20))
        let got = try #require(LedObservation.observe(frames))
        #expect(got.state == .on && got.color == "red" && got.cell == cell)
    }

    @Test func aDarkPictureIsADarkLed() throws {
        let samples = (0..<180).map { LedSample(t: Double($0) / 60, on: false) }
        let got = try #require(LedObservation.observe(BlinkSynth.frames(samples, cell: cell)))
        #expect(got.state == .off && got.color == nil)
    }

    @Test func aBlinkingLedIsBlinkingWithItsColour() throws {
        let samples = BlinkSynth.samples([(false, 300), (true, 400), (false, 400), (true, 400), (false, 400), (true, 400), (false, 300)])
        let got = try #require(LedObservation.observe(BlinkSynth.frames(samples, cell: cell, rgb: (30, 255, 40))))
        #expect(got.state == .blinking && got.color == "green")
    }

    @Test func colourNamesFollowTheHue() {
        #expect(HueName.name(r: 255, g: 0, b: 0) == "red" && HueName.name(r: 255, g: 160, b: 0) == "orange")
        #expect(HueName.name(r: 255, g: 255, b: 0) == "yellow" && HueName.name(r: 0, g: 255, b: 0) == "green")
        #expect(HueName.name(r: 0, g: 255, b: 255) == "cyan" && HueName.name(r: 0, g: 0, b: 255) == "blue")
        #expect(HueName.name(r: 200, g: 0, b: 255) == "purple" && HueName.name(r: 255, g: 0, b: 150) == "pink")
        #expect(HueName.name(r: 250, g: 250, b: 245) == "white" && HueName.name(r: 0, g: 0, b: 0) == "white")
    }

    // MARK: The pixel reduction

    @Test func aSmallBrightSpotSurvivesTheReductionAndKeepsItsColour() {
        let w = 640, h = 480
        var pixels = [UInt8](repeating: 10, count: w * h * 4)   // BGRA, dark grey
        for y in 100..<106 {
            for x in 200..<206 {
                let o = (y * w + x) * 4
                pixels[o] = 20; pixels[o + 1] = 240; pixels[o + 2] = 30; pixels[o + 3] = 255   // green
            }
        }
        let reduced = pixels.withUnsafeBufferPointer { p in
            CellGrid.reduce(base: p.baseAddress!, width: w, height: h, bytesPerRow: w * 4)
        }
        let cell = (100 * CellGrid.rows / h) * CellGrid.cols + 200 * CellGrid.cols / w
        #expect(reduced.luma[cell] > 0.5)
        #expect(reduced.rgb[cell * 3 + 1] > 200 && reduced.rgb[cell * 3] < 60)
        #expect(reduced.luma[0] < 0.1 && reduced.mean < 0.1)
    }
}
