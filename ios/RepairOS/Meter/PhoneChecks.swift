@preconcurrency import AVFoundation
import Combine
import CoreMotion
import Foundation

// The phone's own instruments, each as a small object the sheet watches. They only start when the person taps the
// instrument, so the camera and microphone prompts appear then and not before. None of this runs in the simulator
// (no camera, microphone or magnetometer there); the rules they feed are unit-tested on synthetic data.
//
// Every object numbers its runs (`generation`): stopping or starting again makes the older run's results, cleanup and
// camera calls void, so a run that wakes up late can never stop the camera of the run that replaced it.

/// Reads the product's status LED through the camera: finds the LED, turns its brightness into on and off, decodes
/// the blink code, and says how far it has got.
@MainActor
final class BlinkCheck: ObservableObject {
    enum Phase: Equatable { case idle, starting, watching, complete, failed(String) }

    @Published private(set) var phase = Phase.idle
    @Published private(set) var analysis = BlinkAnalysis.Result(progress: .searching, marker: nil)
    @Published private(set) var fps = 0.0
    let code: BlinkCode
    private let probe: CameraProbe
    var session: AVCaptureSession { probe.session }
    private var loop: Task<Void, Never>?
    private var generation = 0

    /// The camera keeps two rounds of the code plus a margin, so a code with many checks still has its preamble in view
    /// when its end flashes arrive.
    init(code: BlinkCode, checkCount: Int) {
        self.code = code
        probe = CameraProbe(keepSeconds: BlinkCheck.windowSeconds(code: code, checkCount: checkCount))
    }

    nonisolated static func windowSeconds(code: BlinkCode, checkCount: Int) -> Double {
        min(300, 2 * code.roundSeconds(checks: checkCount) + 10)
    }

    func start() {
        guard loop == nil else { return }
        generation += 1
        let mine = generation
        phase = .starting
        analysis = BlinkAnalysis.Result(progress: .searching, marker: nil)
        loop = Task { [weak self] in await self?.run(mine) }
    }

    func stop() {
        generation += 1
        loop?.cancel()
        loop = nil
        probe.stop()
        if phase == .starting || phase == .watching { phase = .idle }
    }

    private func run(_ mine: Int) async {
        do {
            let rate = try await probe.start(targetFPS: 60)
            guard mine == generation else { return }
            fps = rate
        } catch {
            guard mine == generation, (error as? CameraProbeError) != .stopped else { return }
            phase = .failed(PhoneCheckWords.camera(error))
            loop = nil
            return
        }
        phase = .watching
        let code = self.code
        while mine == generation {
            try? await Task.sleep(for: .milliseconds(200))
            guard mine == generation else { return }
            let frames = probe.buffer.snapshot()
            let result = await Task.detached(priority: .userInitiated) { BlinkAnalysis.run(frames, code: code) }.value
            guard mine == generation else { return }
            analysis = result
            if case .complete = result.progress {
                phase = .complete
                probe.stop()
                loop = nil
                return
            }
        }
    }

    /// "Frame the LED. 60 frames a second." and similar, for the line under the preview.
    var status: String {
        switch phase {
        case .idle: return "Not started."
        case .starting: return "Starting the camera\u{2026}"
        case .failed(let m): return m
        case .complete: return "Read the whole code."
        case .watching:
            switch analysis.progress {
            case .searching:
                return analysis.marker == nil ? "Hold the phone steady with the status light in view."
                                              : "Waiting for the start of the code (three quick flashes)."
            case .reading(let done): return "Read \(done.count) check\(done.count == 1 ? "" : "s") so far\u{2026}"
            case .complete: return "Read the whole code."
            case .unreadable(let why): return why + " Keep watching: the next round starts again."
            }
        }
    }
}

/// How fast a light flickers, from 2 seconds of the camera's average brightness at the highest frame rate it offers.
@MainActor
final class FlickerCheck: ObservableObject {
    enum Phase: Equatable { case idle, measuring, done, failed(String) }

    @Published private(set) var phase = Phase.idle
    @Published private(set) var result: FlickerAnalysis.Result?
    private let probe = CameraProbe(keepSeconds: 4)
    var session: AVCaptureSession { probe.session }
    private var task: Task<Void, Never>?
    private var generation = 0

    func measure() {
        guard task == nil else { return }
        generation += 1
        let mine = generation
        phase = .measuring
        result = nil
        task = Task { [weak self] in await self?.run(mine) }
    }

    func stop() {
        generation += 1
        task?.cancel()
        task = nil
        probe.stop()
        if phase == .measuring { phase = .idle }
    }

    private func run(_ mine: Int) async {
        defer { if mine == generation { task = nil } }
        do {
            _ = try await probe.start(targetFPS: 240)
        } catch {
            guard mine == generation, (error as? CameraProbeError) != .stopped else { return }
            phase = .failed(PhoneCheckWords.camera(error))
            return
        }
        try? await Task.sleep(for: .milliseconds(700))   // the camera's exposure settles
        guard mine == generation else { return }
        probe.buffer.clear()
        try? await Task.sleep(for: .seconds(2))
        guard mine == generation else { return }
        let frames = probe.buffer.snapshot()
        probe.stop()
        let got = await Task.detached { FlickerAnalysis.analyze(frames: frames) }.value
        guard mine == generation else { return }
        if let got {
            result = got
            phase = .done
        } else {
            phase = .failed("The camera gave too few frames to measure. Try again.")
        }
    }
}

/// On, dim or off against a baseline taken with the product off, from the camera's own exposure.
@MainActor
final class LightCheck: ObservableObject {
    enum Phase: Equatable { case idle, measuring, failed(String) }

    @Published private(set) var phase = Phase.idle
    @Published private(set) var baseline: Double?
    @Published private(set) var ratio: Double?
    @Published private(set) var state: ValueState?
    private let probe = CameraProbe(keepSeconds: 3)
    var session: AVCaptureSession { probe.session }
    private var task: Task<Void, Never>?
    private var generation = 0

    /// Point the camera at the part with the product off.
    func takeBaseline() {
        guard task == nil else { return }
        ratio = nil
        state = nil
        run { [weak self] level in self?.baseline = level }
    }

    /// With the product on, same view.
    func measure() {
        guard task == nil, let base = baseline else { return }
        run { [weak self] level in
            guard let self else { return }
            let r = level / base
            self.ratio = r
            self.state = RelativeLight.state(ratio: r)
        }
    }

    func stop() {
        generation += 1
        task?.cancel()
        task = nil
        probe.stop()
        if phase == .measuring { phase = .idle }
    }

    private func run(_ done: @escaping @MainActor (Double) -> Void) {
        generation += 1
        let mine = generation
        phase = .measuring
        task = Task { [weak self] in
            guard let self else { return }
            defer { if mine == self.generation { self.task = nil } }
            do {
                _ = try await self.probe.start(targetFPS: 30)
            } catch {
                guard mine == self.generation, (error as? CameraProbeError) != .stopped else { return }
                self.phase = .failed(PhoneCheckWords.camera(error))
                return
            }
            try? await Task.sleep(for: .milliseconds(900))   // the camera's exposure settles
            guard mine == self.generation else { return }
            self.probe.buffer.clear()
            try? await Task.sleep(for: .seconds(1))
            guard mine == self.generation else { return }
            let frames = self.probe.buffer.snapshot()
            self.probe.stop()
            let levels = frames.compactMap { RelativeLight.level(meanBrightness: $0.mean, iso: $0.iso, exposureS: $0.exposureS) }.sorted()
            guard !levels.isEmpty else {
                self.phase = .failed("The camera did not report its exposure. Try again.")
                return
            }
            self.phase = .idle
            done(levels[levels.count / 2])
        }
    }
}

/// What the camera sees an LED do: lit, dark or blinking, and its colour.
@MainActor
final class LedStateCheck: ObservableObject {
    enum Phase: Equatable { case idle, watching, done, failed(String) }

    @Published private(set) var phase = Phase.idle
    @Published private(set) var result: LedObservation.Result?
    private let probe = CameraProbe(keepSeconds: 5)
    var session: AVCaptureSession { probe.session }
    private var task: Task<Void, Never>?
    private var generation = 0

    func watch() {
        guard task == nil else { return }
        generation += 1
        let mine = generation
        phase = .watching
        result = nil
        task = Task { [weak self] in await self?.run(mine) }
    }

    func stop() {
        generation += 1
        task?.cancel()
        task = nil
        probe.stop()
        if phase == .watching { phase = .idle }
    }

    private func run(_ mine: Int) async {
        defer { if mine == generation { task = nil } }
        do {
            _ = try await probe.start(targetFPS: 60)
        } catch {
            guard mine == generation, (error as? CameraProbeError) != .stopped else { return }
            phase = .failed(PhoneCheckWords.camera(error))
            return
        }
        try? await Task.sleep(for: .milliseconds(700))
        guard mine == generation else { return }
        probe.buffer.clear()
        try? await Task.sleep(for: .seconds(3))
        guard mine == generation else { return }
        let frames = probe.buffer.snapshot()
        probe.stop()
        let got = await Task.detached { LedObservation.observe(frames) }.value
        guard mine == generation else { return }
        if let got {
            result = got
            phase = .done
        } else {
            phase = .failed("The camera gave too few frames. Try again.")
        }
    }
}

/// The change in the magnetic field next to a part when the product is switched on: a 2 second baseline with it off,
/// then a live number.
@MainActor
final class MagnetometerCheck: ObservableObject {
    enum Phase: Equatable { case idle, baseline, live, failed(String) }

    @Published private(set) var phase = Phase.idle
    /// 0 to 1 while the baseline is being taken.
    @Published private(set) var progress = 0.0
    @Published private(set) var baseline: MagneticBaseline?
    @Published private(set) var delta: Double?
    @Published private(set) var state: ValueState?
    private let motion = CMMotionManager()
    private var task: Task<Void, Never>?
    private var generation = 0

    static let baselineSeconds = 2.0
    private static let intervalS = 0.02

    func start() {
        guard task == nil else { return }
        guard motion.isMagnetometerAvailable else {
            phase = .failed(PhoneCheckWords.noMagnetometer)
            return
        }
        generation += 1
        let mine = generation
        baseline = nil
        delta = nil
        state = nil
        progress = 0
        phase = .baseline
        motion.magnetometerUpdateInterval = Self.intervalS
        motion.startMagnetometerUpdates()
        task = Task { [weak self] in await self?.run(mine) }
    }

    func stop() {
        generation += 1
        task?.cancel()
        task = nil
        motion.stopMagnetometerUpdates()
        if phase == .baseline || phase == .live { phase = .idle }
    }

    private func next(after last: inout TimeInterval) -> Double? {
        guard let d = motion.magnetometerData, d.timestamp != last else { return nil }
        last = d.timestamp
        return Magnetometer.magnitude(x: d.magneticField.x, y: d.magneticField.y, z: d.magneticField.z)
    }

    private func run(_ mine: Int) async {
        var last: TimeInterval = -1
        var samples: [Double] = []
        let began = Date()
        while mine == generation, Date().timeIntervalSince(began) < Self.baselineSeconds {
            if let m = next(after: &last) { samples.append(m) }
            progress = min(1, Date().timeIntervalSince(began) / Self.baselineSeconds)
            try? await Task.sleep(for: .milliseconds(20))
        }
        guard mine == generation else { return }
        guard let base = MagneticBaseline(magnitudes: samples) else {
            phase = .failed("The magnetometer gave too few readings. Try again.")
            motion.stopMagnetometerUpdates()
            task = nil
            return
        }
        baseline = base
        phase = .live
        var recent: [Double] = []
        while mine == generation {
            if let m = next(after: &last) {
                recent.append(m)
                if recent.count > 50 { recent.removeFirst(recent.count - 50) }   // the last second
                if let d = Magnetometer.delta(baseline: base, recent: recent) {
                    delta = d
                    state = Magnetometer.state(delta: d, baseline: base)
                }
            }
            try? await Task.sleep(for: .milliseconds(20))
        }
    }
}

/// The sound of a motor or fan: the live fundamental from the microphone. Voice-assistant processing is not used, so
/// the tone is not filtered out as noise. "Hey Lea" listening is paused while this runs.
@MainActor
final class AudioCheck: ObservableObject {
    enum Phase: Equatable { case idle, starting, listening, failed(String) }

    @Published private(set) var phase = Phase.idle
    @Published private(set) var tone: SpectralPeak?
    @Published private(set) var levelDB: Float = -120
    /// The tone once the last few estimates agree; nil while it wanders or there is none.
    @Published private(set) var steady: Double?
    private var engine: AVAudioEngine?
    private let ring = SampleRing(capacity: 32768)
    private var task: Task<Void, Never>?
    private var estimates: [Double] = []
    private var generation = 0
    /// "Hey Lea" is paused by this check (and must be given back).
    private var heldWake = false
    private static let window = 16384

    func start() {
        guard task == nil else { return }
        generation += 1
        let mine = generation
        phase = .starting
        tone = nil
        steady = nil
        estimates = []
        task = Task { [weak self] in await self?.run(mine) }
    }

    func stop() {
        generation += 1
        task?.cancel()
        task = nil
        engine?.inputNode.removeTap(onBus: 0)
        engine?.stop()
        engine = nil
        giveBackWake()
        if phase == .starting || phase == .listening { phase = .idle }
    }

    private func giveBackWake() {
        guard heldWake else { return }
        heldWake = false
        WakeListener.shared.suspend(false)
    }

    private func fail(_ mine: Int, _ text: String) {
        guard mine == generation else { return }
        giveBackWake()
        phase = .failed(text)
        task = nil
    }

    private func run(_ mine: Int) async {
        switch MicPermission.state {
        case .denied:
            fail(mine, PhoneCheckWords.microphoneDenied)
            return
        case .undetermined:
            let granted = await MicPermission.request()
            guard mine == generation else { return }
            if !granted {
                fail(mine, PhoneCheckWords.microphoneDenied)
                return
            }
        case .granted: break
        }
        WakeListener.shared.suspend(true)
        heldWake = true
        let recording = await VoiceAudioSession.record()
        guard mine == generation else { return }   // stop() already gave "Hey Lea" back
        guard recording else {
            fail(mine, PhoneCheckWords.microphoneFailed)
            return
        }
        let engine = AVAudioEngine()
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0 else {
            fail(mine, PhoneCheckWords.microphoneFailed)
            return
        }
        let ring = self.ring
        ring.clear()
        input.installTap(onBus: 0, bufferSize: 4096, format: format) { buffer, _ in
            guard let channel = buffer.floatChannelData?[0] else { return }
            ring.append(UnsafeBufferPointer(start: channel, count: Int(buffer.frameLength)))
        }
        do {
            try engine.start()
        } catch {
            input.removeTap(onBus: 0)
            fail(mine, PhoneCheckWords.microphoneFailed)
            return
        }
        self.engine = engine
        phase = .listening
        let rate = format.sampleRate
        while mine == generation {
            try? await Task.sleep(for: .milliseconds(250))
            guard mine == generation else { return }
            guard let samples = ring.latest(Self.window) else { continue }
            let got = await Task.detached(priority: .userInitiated) { (AudioAnalysis.tone(samples, rate: rate), AudioAnalysis.level(samples)) }.value
            guard mine == generation else { return }
            tone = got.0
            levelDB = got.1
            if let hz = got.0?.hz { estimates.append(hz) } else { estimates = [] }
            steady = AudioAnalysis.steady(estimates)
        }
    }
}

/// The latest microphone samples, written from the audio thread and read from the main actor.
final class SampleRing: @unchecked Sendable {
    private let lock = NSLock()
    private var samples: [Float] = []
    private let capacity: Int

    init(capacity: Int) { self.capacity = capacity }

    func append(_ chunk: UnsafeBufferPointer<Float>) {
        lock.lock()
        defer { lock.unlock() }
        samples.append(contentsOf: chunk)
        if samples.count > capacity { samples.removeFirst(samples.count - capacity) }
    }

    /// The newest `n` samples, or nil until that many have arrived.
    func latest(_ n: Int) -> [Float]? {
        lock.lock()
        defer { lock.unlock() }
        return samples.count >= n ? Array(samples.suffix(n)) : nil
    }

    func clear() {
        lock.lock()
        defer { lock.unlock() }
        samples.removeAll()
    }
}
