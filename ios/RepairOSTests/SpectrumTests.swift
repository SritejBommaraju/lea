import Foundation
import Testing
@testable import RepairOS

/// The FFT and what is read from it, on synthetic signals whose answer is known: a tone's fundamental (a motor's sound),
/// a light's flicker frequency and depth. The microphone and the camera cannot run in the simulator.
struct SpectrumTests {
    static let size = 4096
    static let rate = 4096.0   // one hertz per bin

    static func signal(_ parts: [(hz: Double, amplitude: Double)], rate: Double = SpectrumTests.rate, size: Int = SpectrumTests.size, offset: Double = 0) -> [Float] {
        (0..<size).map { i in
            let t = Double(i) / rate
            return Float(offset + parts.reduce(0) { $0 + $1.amplitude * sin(2 * Double.pi * $1.hz * t) })
        }
    }

    static func amplitudes(_ x: [Float]) -> [Float] { Spectrum(size: x.count)!.amplitudes(x) }

    @Test func onlyAPowerOfTwoMakesASpectrum() {
        #expect(Spectrum(size: 100) == nil && Spectrum(size: 8) == nil)
        #expect(Spectrum(size: 4096) != nil)
    }

    @Test func aSineOfKnownAmplitudeComesBackAtItsFrequencyAndSize() throws {
        let amp = Self.amplitudes(Self.signal([(200, 0.5)]))
        let p = try #require(SpectralTone.peak(amp, rate: Self.rate, size: Self.size, minHz: 30, maxHz: 1800))
        #expect(abs(p.hz - 200) < 0.2 && abs(p.amplitude - 0.5) < 0.025)
    }

    @Test func aToneBetweenTwoBinsIsPlacedBetweenThem() throws {
        let amp = Self.amplitudes(Self.signal([(200.5, 0.5)]))
        let p = try #require(SpectralTone.peak(amp, rate: Self.rate, size: Self.size, minHz: 30, maxHz: 1800))
        #expect(abs(p.hz - 200.5) < 0.3 && abs(p.amplitude - 0.5) < 0.06)
    }

    @Test func aMotorsStrongestLineIsNotItsFundamental() throws {
        // 100 Hz, 200 Hz (the loudest) and 300 Hz: the motor turns at 100.
        let amp = Self.amplitudes(Self.signal([(100, 0.4), (200, 1.0), (300, 0.5)]))
        let f = try #require(SpectralTone.fundamental(amp, rate: Self.rate, size: Self.size, minHz: 30, maxHz: 1800))
        #expect(abs(f.hz - 100) < 1)
        // a pure tone is its own fundamental
        let pure = Self.amplitudes(Self.signal([(200, 0.5)]))
        #expect(abs(try #require(SpectralTone.fundamental(pure, rate: Self.rate, size: Self.size, minHz: 30, maxHz: 1800)).hz - 200) < 1)
    }

    @Test func noiseHasNoTone() {
        var seed: UInt64 = 42
        let noise: [Float] = (0..<Self.size).map { _ in
            seed = seed &* 6364136223846793005 &+ 1442695040888963407
            return (Float(seed >> 40) / Float(1 << 24) * 2 - 1) * 0.5
        }
        #expect(SpectralTone.peak(Self.amplitudes(noise), rate: Self.rate, size: Self.size, minHz: 30, maxHz: 1800) == nil)
        #expect(AudioAnalysis.tone(noise, rate: Self.rate) == nil)
    }

    @Test func theAudioHelperFindsTheFundamentalAndTheLevel() throws {
        let x = Self.signal([(120, 0.3)])
        #expect(abs(try #require(AudioAnalysis.tone(x, rate: Self.rate)).hz - 120) < 1)
        #expect(abs(AudioAnalysis.level([Float](repeating: 0.1, count: 64)) - (-20)) < 0.01)
        #expect(AudioAnalysis.level([]) == -120)
    }

    @Test func aToneIsSteadyOnlyWhenTheLastEstimatesAgree() {
        #expect(AudioAnalysis.steady([100, 100.5, 101, 100.2, 100.8]) == 100.5)
        #expect(AudioAnalysis.steady([100, 110, 100, 100, 100]) == nil)
        #expect(AudioAnalysis.steady([100, 100, 100]) == nil)
        #expect(AudioAnalysis.steady([90, 100, 100.5, 101, 100.2, 100.8]) == 100.5)   // only the last five count
    }

    // MARK: Flicker

    private func flicker(hz: Double, depth: Double, fps: Double = 240, frames: Int = 512) -> FlickerAnalysis.Result? {
        let t = (0..<frames).map { Double($0) / fps }
        let b = t.map { Float(1 + depth * sin(2 * Double.pi * hz * $0)) }
        return FlickerAnalysis.analyze(brightness: b, t: t)
    }

    @Test func aFlickeringLightGivesItsFrequencyAndDepth() throws {
        let a = try #require(flicker(hz: 50, depth: 0.35))
        #expect(abs(a.hz - 50) < 1 && abs(a.depth - 0.35) < 0.05)
        #expect(abs(a.rate - 240) < 0.01 && abs(a.limitHz - 108) < 0.1)
        let b = try #require(flicker(hz: 100, depth: 0.2))
        #expect(abs(b.hz - 100) < 1 && abs(b.depth - 0.2) < 0.04)
    }

    @Test func aSteadyLightHasNoFlicker() throws {
        let r = try #require(flicker(hz: 50, depth: 0))
        #expect(r.hz == 0 && r.depth == 0)
    }

    @Test func tooFewFramesGiveNoAnswer() {
        #expect(flicker(hz: 50, depth: 0.3, frames: 40) == nil)
    }

    @Test func theFrameRateComesFromTheFrameTimesNotFromTheRequest() throws {
        // A phone that gave 120 frames a second although 240 was asked for.
        let a = try #require(flicker(hz: 30, depth: 0.4, fps: 120, frames: 256))
        #expect(abs(a.rate - 120) < 0.01 && abs(a.limitHz - 54) < 0.1 && abs(a.hz - 30) < 1)
    }
}
