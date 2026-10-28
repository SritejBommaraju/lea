import Accelerate
import Foundation

/// An amplitude spectrum (Hann window, vDSP's real FFT) and what the phone checks read from it: the fundamental of a
/// motor's or fan's sound, and the strongest brightness oscillation of a light. Unit-tested on synthetic signals
/// (`RepairOSTests/SpectrumTests.swift`).
final class Spectrum {
    let size: Int
    private let log2n: vDSP_Length
    private let setup: FFTSetup
    private let window: [Float]

    /// `size` must be a power of two, at least 16.
    init?(size: Int) {
        guard size >= 16, size & (size - 1) == 0 else { return nil }
        let log2n = vDSP_Length(size.trailingZeroBitCount)
        guard let setup = vDSP_create_fftsetup(log2n, FFTRadix(kFFTRadix2)) else { return nil }
        self.size = size
        self.log2n = log2n
        self.setup = setup
        self.window = (0..<size).map { 0.5 - 0.5 * cos(2 * Float.pi * Float($0) / Float(size)) }
    }

    deinit { vDSP_destroy_fftsetup(setup) }

    /// Amplitude per bin, bins 0 ..< size/2 (bin k is k * rate / size hertz). A sine of amplitude A on a bin gives A
    /// (the Hann window's loss and vDSP's factor of two are taken out). The mean (bin 0) is zeroed.
    func amplitudes(_ samples: [Float]) -> [Float] {
        precondition(samples.count == size, "Spectrum.amplitudes needs exactly `size` samples")
        let half = size / 2
        var re = [Float](repeating: 0, count: half)
        var im = [Float](repeating: 0, count: half)
        for i in 0..<half {
            re[i] = samples[2 * i] * window[2 * i]
            im[i] = samples[2 * i + 1] * window[2 * i + 1]
        }
        var out = [Float](repeating: 0, count: half)
        let scale = 2 / Float(size)
        re.withUnsafeMutableBufferPointer { rp in
            im.withUnsafeMutableBufferPointer { ip in
                var split = DSPSplitComplex(realp: rp.baseAddress!, imagp: ip.baseAddress!)
                vDSP_fft_zrip(setup, &split, 1, log2n, FFTDirection(FFT_FORWARD))
                for k in 1..<half { out[k] = (rp[k] * rp[k] + ip[k] * ip[k]).squareRoot() * scale }
            }
        }
        return out
    }
}

struct SpectralPeak: Equatable, Sendable {
    var hz: Double
    var amplitude: Double
}

enum SpectralTone {
    /// The strongest local maximum between `minHz` and `maxHz`, placed between bins by a parabola through it and its
    /// neighbours. nil when the strongest bin is not a local maximum (the band ends on a slope).
    static func localPeak(_ amp: [Float], rate: Double, size: Int, minHz: Double, maxHz: Double) -> SpectralPeak? {
        let binHz = rate / Double(size)
        let lo = max(2, Int((minHz / binHz).rounded(.up)))
        let hi = min(amp.count - 2, Int((maxHz / binHz).rounded(.down)))
        guard lo <= hi else { return nil }
        var k = lo
        for i in lo...hi where amp[i] > amp[k] { k = i }
        guard amp[k] > 0, amp[k] >= amp[k - 1], amp[k] >= amp[k + 1] else { return nil }
        let a = Double(amp[k - 1]), b = Double(amp[k]), c = Double(amp[k + 1])
        let denominator = a - 2 * b + c
        let offset = denominator == 0 ? 0 : 0.5 * (a - c) / denominator
        let clamped = min(0.5, max(-0.5, offset))
        return SpectralPeak(hz: (Double(k) + clamped) * binHz, amplitude: b - 0.25 * (a - c) * clamped)
    }

    /// The typical (median) level of the band: what a peak has to stand above to be a tone and not noise.
    static func bandLevel(_ amp: [Float], rate: Double, size: Int, minHz: Double, maxHz: Double) -> Float {
        let binHz = rate / Double(size)
        let lo = max(2, Int((minHz / binHz).rounded(.up)))
        let hi = min(amp.count - 2, Int((maxHz / binHz).rounded(.down)))
        guard lo <= hi else { return 0 }
        let s = amp[lo...hi].sorted()
        return s[s.count / 2]
    }

    /// The strongest tone of the band, or nil when it does not stand `prominence` times above the band's typical
    /// level (a noisy room has no tone).
    static func peak(_ amp: [Float], rate: Double, size: Int, minHz: Double, maxHz: Double, prominence: Float = 6) -> SpectralPeak? {
        guard let p = localPeak(amp, rate: rate, size: size, minHz: minHz, maxHz: maxHz) else { return nil }
        let typical = bandLevel(amp, rate: rate, size: size, minHz: minHz, maxHz: maxHz)
        guard typical <= 0 || Float(p.amplitude) >= prominence * typical else { return nil }
        return p
    }

    /// The lowest frequency that explains the strongest peak: a motor's strongest line is often its 2nd or 3rd
    /// harmonic. A divisor of 2 to 5 counts when there is a real peak at that fraction of its frequency, at least a
    /// quarter as strong as the strongest and well above the band's typical level.
    static func fundamental(_ amp: [Float], rate: Double, size: Int, minHz: Double, maxHz: Double) -> SpectralPeak? {
        guard let strongest = peak(amp, rate: rate, size: size, minHz: minHz, maxHz: maxHz) else { return nil }
        let typical = bandLevel(amp, rate: rate, size: size, minHz: minHz, maxHz: maxHz)
        for d in stride(from: 5, through: 2, by: -1) {
            let f = strongest.hz / Double(d)
            guard f >= minHz else { continue }
            if let sub = localPeak(amp, rate: rate, size: size, minHz: f * 0.97, maxHz: f * 1.03),
               sub.amplitude >= 0.25 * strongest.amplitude, Float(sub.amplitude) >= 6 * typical {
                return sub
            }
        }
        return strongest
    }
}

enum FlickerAnalysis {
    struct Result: Equatable, Sendable {
        /// Frequency of the strongest oscillation in hertz; 0 when the light is steady.
        var hz: Double
        /// The oscillation's amplitude over the average level, 0 to 1: 0 steady, 1 fully on and off.
        var depth: Double
        var rate: Double
        /// Highest frequency this camera's frame rate can show (the peak is not trusted above it).
        var limitHz: Double
    }

    /// `brightness` is linear light, one value per frame, `t` the frame times in seconds. The camera's real frame rate
    /// is taken from the times. nil when there are too few frames.
    static func analyze(brightness: [Float], t: [Double], minHz: Double = 5) -> Result? {
        guard brightness.count == t.count, brightness.count >= 64, let first = t.first, let last = t.last, last > first else { return nil }
        let rate = Double(t.count - 1) / (last - first)
        var size = 64
        while size * 2 <= brightness.count { size *= 2 }
        let used = Array(brightness.suffix(size))
        let mean = used.reduce(0, +) / Float(size)
        guard mean > 0, let spectrum = Spectrum(size: size) else { return nil }
        let amp = spectrum.amplitudes(used.map { $0 - mean })
        let limit = 0.45 * rate
        guard let p = SpectralTone.peak(amp, rate: rate, size: size, minHz: minHz, maxHz: limit, prominence: 8) else {
            return Result(hz: 0, depth: 0, rate: rate, limitHz: limit)
        }
        return Result(hz: p.hz, depth: min(1, p.amplitude / Double(mean)), rate: rate, limitHz: limit)
    }

    /// From camera frames: each frame's average brightness, with the picture's gamma taken out, over the frames' times.
    static func analyze(frames: [CameraFrame]) -> Result? {
        analyze(brightness: frames.map { Float(pow(Double($0.mean), 2.2)) }, t: frames.map(\.t))
    }
}

enum AudioAnalysis {
    /// The fundamental of the loudest steady sound in `samples` (a power-of-two count), between 30 Hz and 3 kHz, or nil
    /// when nothing stands out of the room's noise.
    static func tone(_ samples: [Float], rate: Double) -> SpectralPeak? {
        guard let spectrum = Spectrum(size: samples.count) else { return nil }
        let amp = spectrum.amplitudes(samples)
        return SpectralTone.fundamental(amp, rate: rate, size: samples.count, minHz: 30, maxHz: min(3000, 0.45 * rate))
    }

    /// Level in decibels relative to full scale (0 is the loudest the microphone can record).
    static func level(_ samples: [Float]) -> Float {
        guard !samples.isEmpty else { return -120 }
        let rms = (samples.reduce(0) { $0 + $1 * $1 } / Float(samples.count)).squareRoot()
        return rms > 0 ? 20 * log10(rms) : -120
    }

    /// A reading to send: the last estimates agree to within 3 %, so the tone is steady. Returns their median.
    static func steady(_ estimates: [Double], needed: Int = 5) -> Double? {
        guard estimates.count >= needed else { return nil }
        let recent = Array(estimates.suffix(needed)).sorted()
        guard let lo = recent.first, let hi = recent.last, lo > 0, (hi - lo) / lo <= 0.03 else { return nil }
        return recent[recent.count / 2]
    }
}
