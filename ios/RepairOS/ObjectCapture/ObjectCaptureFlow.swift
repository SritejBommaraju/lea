import Foundation
import Observation
import RealityKit
import SwiftUI

/// LiDAR phones: Apple's ObjectCaptureSession does the scan (object detection, the adjustable box, background
/// rejection, guidance and auto-capture). Quick by default: one orbit, ended at `quickImages` shots or when Apple
/// says the pass is complete, then straight to close-ups in the same session with `requestImageCapture()`, so they
/// land in the same image set and get poses from the same reconstruction. We need poses and a coarse cloud to
/// locate findings, not a full model; the photos find the problems.
/// Images: `<session>/oc/Images`, checkpoint: `<session>/oc/Checkpoint`.
@MainActor
@Observable
final class ObjectCaptureFlow {
    enum Step: Equatable {
        case starting, ready, detecting, capturing, closeups, finishing, photosOnly
        case reconstructing(String)
        case done
        case failed(String)
    }

    private(set) var step: Step = .starting
    private(set) var session: ObjectCaptureSession?
    private(set) var shots = 0
    private(set) var closeupShots = 0
    private(set) var feedback: Set<ObjectCaptureSession.Feedback> = []
    private(set) var tracking = "unknown"
    private(set) var passDone = false
    private(set) var canShoot = false
    private(set) var progress: Double = 0
    /// Set when Apple refused to start detecting (tracking not settled yet); cleared on the next state change.
    private(set) var notReady = false
    /// Low light was active when the person tapped "Start scan": ask once, with a continue-anyway.
    private(set) var lightBlocked = false
    private var lightAccepted = false
    /// Detection has not locked for `skipAfter` seconds: offer "Skip 3D, just take photos".
    private(set) var offerPhotosOnly = false
    /// Bumped whenever the cue words change to a warning, for a haptic.
    private(set) var cueChanges = 0
    private var lastCue: String?
    private var detectingSince: Date?
    private var firstImageAt: Date?

    /// One orbit is enough for poses and a coarse cloud.
    static let quickImages = 22
    static let skipAfter: TimeInterval = 8

    private(set) var record: SessionRecord
    let storage: ScanStorage
    let upload: UploadQueue?
    let diag: Diag
    private var watchers: [Task<Void, Never>] = []
    private var heartbeat: Task<Void, Never>?
    private var filesBeforeCloseups: Set<String> = []
    private var startedAt = Date()

    var imagesDir: URL { storage.dir(record).appending(path: "oc/Images", directoryHint: .isDirectory) }
    var checkpointDir: URL { storage.dir(record).appending(path: "oc/Checkpoint", directoryHint: .isDirectory) }

    static var isSupported: Bool { ObjectCaptureSession.isSupported }

    private var launchedAt: Date
    private var capturingSince: Date?

    init(record: SessionRecord, storage: ScanStorage, upload: UploadQueue?, launchedAt: Date) {
        self.launchedAt = launchedAt
        self.record = record
        self.storage = storage
        self.upload = upload
        diag = Diag.session(storage.dir(record))
    }

    func start() {
        guard session == nil, step == .starting else { return }
        do {
            try FileManager.default.createDirectory(at: imagesDir, withIntermediateDirectories: true)
            try FileManager.default.createDirectory(at: checkpointDir, withIntermediateDirectories: true)
        } catch {
            fail("Could not create the image folder: \(error.localizedDescription)")
            return
        }
        let s = ObjectCaptureSession()
        var config = ObjectCaptureSession.Configuration()
        config.checkpointDirectory = checkpointDir
        config.isOverCaptureEnabled = false
        s.start(imagesDirectory: imagesDir, configuration: config)
        session = s
        startedAt = Date()
        diag.log("oc_start", ["max_images": s.maximumNumberOfInputImages,
                              "since_launch_s": Int(Date().timeIntervalSince(launchedAt)),
                              "model": DeviceCapabilities.current().model])
        watch(s)
        onState(s.state)
        heartbeat = Task { [weak self] in
            var tick = 0
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(1))
                guard let self else { return }
                tick += 1
                // stateUpdates does not replay the current state; poll it so a missed transition never strands the UI.
                if let s = self.session, s.state != self.lastState { self.onState(s.state) }
                if self.step == .detecting, !self.offerPhotosOnly, let since = self.detectingSince,
                   Date().timeIntervalSince(since) >= Self.skipAfter, self.feedback.contains(.objectNotDetected) {
                    self.offerPhotosOnly = true
                    self.diag.log("offer_photos_only", ["after_s": Int(Date().timeIntervalSince(since))])
                }
                guard tick % 5 == 0 else { continue }
                // Tracking is sampled here: its update stream's element type is not Sendable.
                if let s = self.session {
                    let t = "\(s.cameraTracking)"
                    if t != self.tracking { self.diag.log("tracking", ["value": t]) }
                    self.tracking = t
                }
                self.diag.log("heartbeat", ["step": "\(self.step)", "shots": self.shots, "tracking": self.tracking])
            }
        }
    }

    /// The one primary button: what it does depends on Apple's state.
    var primaryTitle: String? {
        switch step {
        case .ready: "Find object"
        // Only once Apple has found an object: capturing without one scans the whole scene.
        case .detecting: feedback.contains(.objectNotDetected) ? nil : "Start scan"
        case .capturing: "Close-ups"
        case .closeups: "Finish"
        default: nil
        }
    }

    func primary() {
        guard let session else { return }
        switch step {
        case .ready:
            let ok = session.startDetecting()
            notReady = !ok
            diag.log("start_detecting", ["ok": ok])
        case .detecting:
            let dark = feedback.contains(.environmentLowLight) || feedback.contains(.environmentTooDark)
            if dark, !lightAccepted {
                lightBlocked = true
                diag.log("light_block")
                return
            }
            session.startCapturing()
            capturingSince = Date()
            diag.log("start_capturing", ["since_launch_s": Int(Date().timeIntervalSince(launchedAt)), "low_light": dark,
                                         "path": "object_capture"])
        case .capturing:
            enterCloseups()
        case .closeups:
            finishCapture()
        default:
            break
        }
    }

    /// The photos-only path finished: its record (captured, stills queued) replaces ours.
    func photosDone(_ r: SessionRecord) {
        record = r
        step = .done
    }

    func continueInLowLight() {
        lightAccepted = true
        lightBlocked = false
        diag.log("light_continue_anyway")
        primary()
    }

    /// The object will not lock (small, glossy, dark): drop the 3D scan and take plain photos instead.
    func skipToPhotos() {
        diag.log("path", ["value": "photos_only", "step": "\(step)", "shots": shots])
        session?.cancel()
        teardown()
        heartbeat?.cancel()
        step = .photosOnly
    }

    /// The prioritised one-line cue for Apple's active feedback (nil when nothing is wrong).
    var cue: String? {
        let fb = feedback
        if fb.contains(.objectNotDetected) || fb.contains(.outOfFieldOfView) { return "Point at the object" }
        if fb.contains(.environmentTooDark) || fb.contains(.environmentLowLight) { return "Too dark: turn on a light" }
        if fb.contains(.objectTooClose) { return "Step back a little" }
        if fb.contains(.movingTooFast) { return "Slower" }
        if fb.contains(.objectTooFar) { return "Move closer" }
        return nil
    }

    private func noteCue() {
        let c = cue
        if c != lastCue {
            // One warning buzz when a problem starts, not on every change between problem cues.
            if c != nil && lastCue == nil { cueChanges += 1 }
            lastCue = c
        }
    }

    func shutter() {
        guard let session, session.canRequestImageCapture else { return }
        session.requestImageCapture()
        closeupShots += 1
        diag.log("closeup_requested", ["n": closeupShots])
    }

    func cancel() {
        diag.log("cancel", ["step": "\(step)"])
        session?.cancel()
        teardown()
    }

    private var scanSeconds: Int { capturingSince.map { Int(Date().timeIntervalSince($0)) } ?? -1 }

    /// The app left the foreground (call, lock, app switch): pause capture and resume on return.
    func scenePhase(_ phase: ScenePhase) {
        guard let session else { return }
        switch phase {
        case .background:
            session.pause()
            diag.log("background", ["step": "\(step)", "shots": shots])
        case .active:
            if session.isPaused { session.resume() }
            diag.log("foreground", ["step": "\(step)", "paused": session.isPaused])
        default:
            break
        }
    }

    private func enterCloseups(reason: String = "button") {
        guard step == .capturing else { return }
        filesBeforeCloseups = Set(imageFiles())
        // Only the person's shots from here: auto-capture would keep firing on the orbit (iOS 18+).
        if #available(iOS 18.0, *) { session?.isAutoCaptureEnabled = false }
        step = .closeups
        let perMin = scanSeconds > 0 ? Double(shots) * 60 / Double(scanSeconds) : 0
        diag.log("closeups_start", ["orbit_images": filesBeforeCloseups.count, "shots": shots, "scan_s": scanSeconds,
                                    "images_per_min": (perMin * 10).rounded() / 10, "reason": reason])
    }

    private func finishCapture() {
        step = .finishing
        session?.finish()
        diag.log("finish_requested", ["shots": shots, "closeups": closeupShots, "scan_s": scanSeconds])
    }

    private func watch(_ s: ObjectCaptureSession) {
        watchers.append(Task { [weak self] in
            for await state in s.stateUpdates { self?.onState(state) }
        })
        watchers.append(Task { [weak self] in
            for await fb in s.feedbackUpdates {
                guard let self else { return }
                self.feedback = fb
                self.noteCue()
                self.diag.log("feedback", ["items": fb.map { "\($0)" }.sorted()])
            }
        })
        watchers.append(Task { [weak self] in
            for await done in s.userCompletedScanPassUpdates {
                guard let self else { return }
                self.passDone = done
                self.diag.log("pass_completed", ["value": done])
                if done { self.enterCloseups(reason: "pass_completed") }
            }
        })
        watchers.append(Task { [weak self] in
            for await n in s.numberOfShotsTakenUpdates {
                guard let self else { return }
                self.shots = n
                if n >= 1, self.firstImageAt == nil, let since = self.capturingSince {
                    self.firstImageAt = Date()
                    self.diag.log("first_image", ["after_capture_s": (Date().timeIntervalSince(since) * 10).rounded() / 10])
                }
                if self.step == .capturing, n >= Self.quickImages { self.enterCloseups(reason: "quick_images") }
            }
        })
        watchers.append(Task { [weak self] in
            for await ok in s.canRequestImageCaptureUpdates { self?.canShoot = ok }
        })
    }

    private var lastState: ObjectCaptureSession.CaptureState?

    private func onState(_ state: ObjectCaptureSession.CaptureState) {
        guard state != lastState else { return }
        lastState = state
        notReady = false
        diag.log("oc_state", ["state": "\(state)", "shots": shots])
        switch state {
        case .initializing: step = .starting
        case .ready: step = .ready
        case .detecting:
            step = .detecting
            detectingSince = Date()
        case .capturing: if step != .closeups { step = .capturing }
        case .finishing: step = .finishing
        case .completed:
            // Apple: release the capture session before PhotogrammetrySession runs (both are memory heavy).
            teardown()
            Task { await reconstruct() }
        case .failed(let error):
            fail("Capture failed: \(error.localizedDescription)")
        @unknown default:
            break
        }
    }

    private func teardown() {
        watchers.forEach { $0.cancel() }
        watchers = []
        session = nil
    }

    private func fail(_ message: String) {
        diag.log("error", ["message": message])
        heartbeat?.cancel()
        teardown()
        step = .failed(message)
    }

    private func imageFiles() -> [String] {
        ((try? FileManager.default.contentsOfDirectory(atPath: imagesDir.path)) ?? [])
            .filter { $0.lowercased().hasSuffix(".heic") || $0.lowercased().hasSuffix(".jpg") }.sorted()
    }

    private func reconstruct() async {
        let all = imageFiles()
        let closeups = all.filter { !filesBeforeCloseups.contains($0) && !filesBeforeCloseups.isEmpty }
        try? JSON.encoder.encode(["closeups": closeups]).write(to: imagesDir.deletingLastPathComponent().appending(path: "closeups.json"))
        diag.log("capture_completed", ["images": all.count, "closeups": closeups.count,
                                       "seconds": Int(Date().timeIntervalSince(startedAt))])
        step = .reconstructing("Working out camera positions…")
        do {
            let result = try await Reconstruction(imagesDir: imagesDir, checkpointDir: checkpointDir,
                                                  scanDir: storage.scanDir(record), stillsDir: storage.stillsDir(record),
                                                  record: record, closeupFiles: Set(closeups), diag: diag,
                                                  startedAt: startedAt, device: DeviceCapabilities.current().contract) { [weak self] stage, fraction in
                Task { @MainActor in
                    self?.step = .reconstructing(stage)
                    self?.progress = fraction
                }
            }.run()
            record.stillIDs = result.stillIDs
            record.status = .captured
            try? storage.save(record)
            upload?.enqueueAll(stillIDs: result.stillIDs, scanReady: true)
            diag.log("package_written", ["keyframes": result.keyframes, "stills": result.stillIDs.count, "points": result.points])
            heartbeat?.cancel()
            step = .done
        } catch {
            fail("Reconstruction failed: \(error.localizedDescription)")
        }
    }
}

/// Apple's capture view with our minimal layer: one primary button, one big cue line with a haptic, a shutter in
/// the close-up step, and the photos-only fallback.
struct ObjectCaptureFlowView: View {
    @Bindable var flow: ObjectCaptureFlow
    @Environment(\.scenePhase) private var scenePhase
    @State private var photos: PhotoCapture?
    let close: () -> Void
    let finished: () -> Void

    var body: some View {
        Group {
            if flow.step == .photosOnly, let photos {
                PhotoCaptureView(capture: photos, close: close) { flow.photosDone($0) }
            } else {
                capture
            }
        }
        .onAppear { flow.start() }
        .onChange(of: scenePhase) { _, phase in flow.scenePhase(phase) }
        .onChange(of: flow.step) { _, step in
            if step == .photosOnly, photos == nil {
                photos = PhotoCapture(record: flow.record, storage: flow.storage, upload: flow.upload, diag: flow.diag)
            }
            if step == .done { finished() }
        }
        .statusBarHidden()
    }

    private var capture: some View {
        ZStack {
            if let session = flow.session {
                ObjectCaptureView(session: session).ignoresSafeArea()
            } else {
                Color.black.ignoresSafeArea()
            }
            VStack(spacing: 12) {
                HStack {
                    Button(action: { flow.cancel(); close() }) {
                        Image(systemName: "xmark").font(.headline).padding(10).background(.ultraThinMaterial, in: Circle())
                    }
                    Spacer()
                    if flow.step == .capturing || flow.step == .closeups {
                        Text(flow.step == .capturing ? "\(flow.shots)/\(ObjectCaptureFlow.quickImages)" : "\(flow.shots)")
                            .font(.headline.monospacedDigit())
                            .padding(10).background(.ultraThinMaterial, in: Capsule())
                    }
                }
                Spacer()
                if let words {
                    Text(words)
                        .font(.largeTitle.bold()).foregroundStyle(.white).multilineTextAlignment(.center)
                        .padding(.horizontal, 20).padding(.vertical, 12)
                        .background(.black.opacity(0.7), in: RoundedRectangle(cornerRadius: 18))
                        .animation(.easeInOut(duration: 0.15), value: words)
                }
                buttons
            }
            .padding()
            if flow.lightBlocked { lightBlock }
        }
        .sensoryFeedback(.warning, trigger: flow.cueChanges)
        .sensoryFeedback(.success, trigger: flow.step == .closeups)
    }

    private var words: String? {
        switch flow.step {
        case .starting: "Starting camera…"
        case .ready: flow.notReady ? "Move slowly, then tap again" : "Point at the object"
        case .detecting: flow.cue ?? "Adjust the box if needed"
        case .capturing: flow.cue ?? "Walk slowly around it"
        case .closeups: flow.cue ?? "Tap anything that looks damaged or important"
        case .finishing: "Saving…"
        case .reconstructing(let s): "\(s) \(Int(flow.progress * 100))%"
        case .photosOnly, .done: nil
        case .failed(let m): m
        }
    }

    private var lightBlock: some View {
        VStack(spacing: 16) {
            Text("More light needed").font(.largeTitle.bold())
            Text("Turn on a light or move to a brighter spot. Dim light makes the scan slow.")
                .multilineTextAlignment(.center)
            Button("Continue anyway") { flow.continueInLowLight() }.buttonStyle(.borderedProminent)
        }
        .foregroundStyle(.white)
        .padding(28)
        .background(.black.opacity(0.85), in: RoundedRectangle(cornerRadius: 24))
        .padding()
    }

    @ViewBuilder private var buttons: some View {
        switch flow.step {
        case .closeups:
            HStack(spacing: 20) {
                Button(action: flow.shutter) {
                    Circle().strokeBorder(.white, lineWidth: 4).frame(width: 72, height: 72)
                        .overlay(Circle().fill(.white.opacity(flow.canShoot ? 0.9 : 0.3)).padding(8))
                }
                .disabled(!flow.canShoot)
                Button("Finish") { flow.primary() }.buttonStyle(.borderedProminent)
            }
        case .detecting where flow.offerPhotosOnly:
            VStack(spacing: 10) {
                primaryButton
                Button("Skip 3D, just take photos") { flow.skipToPhotos() }
                    .font(.title3.bold()).buttonStyle(.bordered).tint(.white)
            }
        case .failed:
            Button("Close", action: close).buttonStyle(.borderedProminent)
        case .reconstructing:
            ProgressView(value: flow.progress).tint(.white)
        default:
            primaryButton
        }
    }

    @ViewBuilder private var primaryButton: some View {
        if let title = flow.primaryTitle {
            Button(action: flow.primary) {
                Text(title).font(.title3.bold()).frame(maxWidth: .infinity, minHeight: 52)
            }
            .buttonStyle(.borderedProminent)
        }
    }
}
