import Foundation

/// The product's status-LED blink code, as the report's `self_test.blink` gives it. Durations are milliseconds. The
/// code is: `preambleFlashes` short flashes, a dark gap, then per check its long blinks (1 ok, 2 fail, 3 unknown)
/// with a dark gap between checks, then `endFlashes` short flashes. Pure types: `BlinkDecoder` is unit-tested on
/// synthetic timelines (`RepairOSTests/BlinkDecoderTests.swift`).
struct BlinkCode: Equatable, Sendable {
    var flashOnMS: Double
    var flashOffMS: Double
    var longOnMS: Double
    var longOffMS: Double
    var gapMS: Double
    var preambleFlashes: Int
    var endFlashes: Int
    /// How many long blinks mean ok, fail and unknown (the plan's `long_blinks`, all different).
    var longCountOK: Int
    var longCountFail: Int
    var longCountUnknown: Int
    /// Each measured duration may be this fraction off its nominal one (a camera sees edges to within a frame).
    var tolerance = 0.4

    init(flashOnMS: Double, flashOffMS: Double, longOnMS: Double, longOffMS: Double, gapMS: Double,
         preambleFlashes: Int, endFlashes: Int, longCounts: (ok: Int, fail: Int, unknown: Int)) {
        self.longCountOK = longCounts.ok
        self.longCountFail = longCounts.fail
        self.longCountUnknown = longCounts.unknown
        self.flashOnMS = flashOnMS
        self.flashOffMS = flashOffMS
        self.longOnMS = longOnMS
        self.longOffMS = longOffMS
        self.gapMS = gapMS
        self.preambleFlashes = preambleFlashes
        self.endFlashes = endFlashes
    }

    /// nil when the plan's `long_blinks` is not three different positive counts (the contract requires that; a plan that
    /// breaks it is not read, because a result could be mistaken for another).
    init?(plan blink: Blink) {
        let t = blink.timings
        guard blink.longBlinks.count == 3, Set(blink.longBlinks).count == 3, blink.longBlinks.allSatisfy({ $0 >= 1 }) else { return nil }
        self.init(flashOnMS: Double(t.flashOnMS), flashOffMS: Double(t.flashOffMS), longOnMS: Double(t.longOnMS),
                  longOffMS: Double(t.longOffMS), gapMS: Double(t.gapMS), preambleFlashes: t.preambleFlashes,
                  endFlashes: t.endFlashes,
                  longCounts: (blink.longBlinks[0], blink.longBlinks[1], blink.longBlinks[2]))
    }

    /// The result `count` long blinks mean, or nil when the code has no such result.
    func outcome(forLongCount count: Int) -> SelfTestFrame.Outcome? {
        if count == longCountOK { return .ok }
        if count == longCountFail { return .fail }
        if count == longCountUnknown { return .unknown }
        return nil
    }

    /// Dark time that ends a group of blinks: halfway between the longest gap inside a group and the shortest gap
    /// between groups, each stretched by the tolerance.
    var boundaryMS: Double { (max(longOffMS, flashOffMS) * (1 + tolerance) + gapMS * (1 - tolerance)) / 2 }
}

/// The LED was on or off at time `t` (seconds).
struct LedSample: Equatable, Sendable {
    var t: Double
    var on: Bool
}

/// A brightness reading at time `t` (seconds), 0 to 1.
struct BrightnessSample: Equatable, Sendable {
    var t: Double
    var v: Float
}

enum LedThreshold {
    /// Turns a brightness series into on and off. The levels come from the series itself (its 5th and 95th percentile),
    /// with hysteresis so noise at the edge does not make a blink. nil when the series never changes by `minContrast`:
    /// nothing is blinking.
    static func onOff(_ series: [BrightnessSample], minContrast: Float = 0.06) -> [LedSample]? {
        guard series.count >= 2 else { return nil }
        let sorted = series.map(\.v).sorted()
        let low = sorted[Int(0.05 * Double(sorted.count - 1))]
        let high = sorted[Int(0.95 * Double(sorted.count - 1))]
        let contrast = high - low
        guard contrast >= minContrast else { return nil }
        let up = low + 0.6 * contrast
        let down = low + 0.4 * contrast
        var on = series[0].v > low + 0.5 * contrast
        return series.map { s in
            if on, s.v < down { on = false } else if !on, s.v > up { on = true }
            return LedSample(t: s.t, on: on)
        }
    }

    /// The number of times the series switched between on and off.
    static func edges(_ samples: [LedSample]) -> Int {
        zip(samples, samples.dropFirst()).filter { $0.on != $1.on }.count
    }
}

enum BlinkDecoder {
    struct Pulse: Equatable, Sendable {
        var startMS: Double
        var endMS: Double
        var durationMS: Double { endMS - startMS }
    }

    enum Progress: Equatable, Sendable {
        /// No blink code seen yet (no preamble).
        case searching
        /// The preamble was seen; these checks have been read so far.
        case reading([SelfTestFrame.Outcome])
        /// The preamble, every check and the end flashes were read.
        case complete([SelfTestFrame.Outcome])
        /// Blinks were seen but they do not fit the code, with the reason in plain words.
        case unreadable(String)
    }

    /// The finished on-pulses of a timeline, and how long the LED was dark after the last one (up to now, or up to the
    /// pulse that has just begun; nil when there is none or the pulse on at the start was cut off). An edge is placed halfway between the last sample before it and the first after,
    /// so a missed frame moves an edge by at most half the gap, and a pulse that began before the first sample is dropped.
    static func pulses(_ samples: [LedSample]) -> (pulses: [Pulse], tailDarkMS: Double?) {
        guard var prev = samples.first else { return ([], nil) }
        var pulses: [Pulse] = []
        var start: Double?
        for s in samples.dropFirst() {
            let edge = (prev.t + s.t) / 2 * 1000
            if s.on, !prev.on {
                start = edge
            } else if !s.on, prev.on {
                if let begun = start { pulses.append(Pulse(startMS: begun, endMS: edge)) }
                start = nil
            }
            prev = s
        }
        guard let last = pulses.last else { return (pulses, nil) }
        // Dark time since the last finished pulse: up to now, or up to the pulse that is just starting.
        if prev.on { return (pulses, start.map { $0 - last.endMS }) }
        return (pulses, prev.t * 1000 - last.endMS)
    }

    private enum Length { case short, long, bad }

    private static func length(_ ms: Double, _ c: BlinkCode) -> Length {
        let short = abs(ms - c.flashOnMS) / c.flashOnMS
        let long = abs(ms - c.longOnMS) / c.longOnMS
        if short <= c.tolerance, short <= long { return .short }
        if long <= c.tolerance { return .long }
        return .bad
    }

    /// Reads the code from a timeline that may start anywhere and may still be running. The first preamble that is
    /// followed by a whole, well-formed code wins; blinks that do not fit are skipped to the next preamble.
    static func decode(_ samples: [LedSample], code c: BlinkCode) -> Progress {
        let (pulses, tail) = pulses(samples)
        guard !pulses.isEmpty else { return .searching }
        var groups: [[Pulse]] = []
        for (i, p) in pulses.enumerated() {
            if i == 0 || p.startMS - pulses[i - 1].endMS >= c.boundaryMS {
                groups.append([p])
            } else {
                groups[groups.count - 1].append(p)
            }
        }
        // The last group is only whole once the LED has been dark long enough to end it.
        let lastClosed = (tail ?? 0) >= c.boundaryMS
        let lengths = groups.map { $0.map { length($0.durationMS, c) } }
        func isFlashes(_ i: Int, _ count: Int) -> Bool {
            lengths[i].count == count && lengths[i].allSatisfy { $0 == .short }
        }

        var partial: [SelfTestFrame.Outcome]?
        var failure: String?
        var onlyPreamble = false
        for start in groups.indices where isFlashes(start, c.preambleFlashes) {
            if start == groups.count - 1, !lastClosed { continue }   // maybe still more flashes to come
            var outcomes: [SelfTestFrame.Outcome] = []
            var broken: String?
            var j = start + 1
            walk: while j < groups.count {
                let last = j == groups.count - 1
                if isFlashes(j, c.endFlashes) {
                    // Three flashes straight after three flashes are an end followed by a new start, not an empty code.
                    if outcomes.isEmpty { break walk }
                    return .complete(outcomes)
                }
                // The end flashes may follow the last check's blinks without a long gap: [long...][short x endFlashes].
                let trailing = lengths[j].reversed().prefix { $0 == .short }.count
                if trailing == c.endFlashes, trailing < lengths[j].count,
                   lengths[j].dropLast(trailing).allSatisfy({ $0 == .long }) {
                    guard let o = c.outcome(forLongCount: lengths[j].count - trailing) else {
                        broken = "Check \(outcomes.count + 1) blinked \(lengths[j].count - trailing) times, which is not a result the product sends."
                        break walk
                    }
                    return .complete(outcomes + [o])
                }
                if last, !lastClosed { break walk }
                guard lengths[j].allSatisfy({ $0 == .long }) else {
                    broken = "Blink \(j - start) of the code is not the length the product uses."
                    break walk
                }
                guard let o = c.outcome(forLongCount: groups[j].count) else {
                    broken = "Check \(outcomes.count + 1) blinked \(groups[j].count) times, which is not a result the product sends."
                    break walk
                }
                outcomes.append(o)
                j += 1
            }
            if let broken {
                failure = broken
            } else if outcomes.isEmpty {
                onlyPreamble = true
            } else {
                partial = outcomes
            }
        }
        // A round that got somewhere beats a broken one; a broken one beats a preamble with nothing after it yet.
        if let partial { return .reading(partial) }
        if let failure { return .unreadable(failure) }
        return onlyPreamble ? .reading([]) : .searching
    }
}
