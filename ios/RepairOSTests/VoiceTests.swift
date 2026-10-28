import ARKit
import AVFoundation
import Foundation
import Testing
@testable import RepairOS

struct CueLimiterTests {
    @Test func firstCueSpeaksAtOnce() {
        #expect(CueLimiter().decide("Move closer here.", now: 0, busy: false) == .speak)
    }

    @Test func atMostOneUtteranceEveryFourSeconds() {
        var l = CueLimiter()
        l.spoke("Move closer here.", at: 10)
        #expect(l.decide("Keep it in frame.", now: 11, busy: false) == .wait(until: 14))
        #expect(l.decide("Keep it in frame.", now: 13.99, busy: false) == .wait(until: 14))
        #expect(l.decide("Keep it in frame.", now: 14, busy: false) == .speak)
    }

    @Test func neverOverAnotherUtterance() {
        var l = CueLimiter()
        l.spoke("Move closer here.", at: 0)
        #expect(l.decide("Keep it in frame.", now: 5, busy: true) == .wait(until: 5.5))
        #expect(l.decide("Keep it in frame.", now: 5, busy: false) == .speak)
    }

    /// Greptile PR #31: a scan instruction waits behind a line that must be heard (what the object is) instead of
    /// taking its turn, even once the gap is over and nothing is playing.
    @Test func instructionsWaitBehindAHeldLine() {
        var l = CueLimiter()
        l.spoke("Move closer here.", at: 0)
        #expect(l.decide("Keep it in frame.", now: 2, busy: false, held: true) == .wait(until: 4))
        #expect(l.decide("Keep it in frame.", now: 5, busy: false, held: true) == .wait(until: 5.5))
        #expect(l.decide("Keep it in frame.", now: 5, busy: false, held: false) == .speak)
        #expect(l.decide("Move closer here.", now: 5, busy: false, held: true) == .drop)
    }

    @Test func neverTheSameLineTwiceInTenSeconds() {
        var l = CueLimiter()
        l.spoke("Move closer here.", at: 0)
        #expect(l.decide("Move closer here.", now: 5, busy: false) == .drop)
        #expect(l.decide("Move closer here.", now: 9.9, busy: false) == .drop)
        #expect(l.decide("Move closer here.", now: 10, busy: false) == .speak)
    }

    @Test func otherSpeechCountsForTheGapButNotForRepeats() {
        var l = CueLimiter()
        l.spoke(nil, at: 0)   // the report summary or an answer
        #expect(l.decide("Got it.", now: 2, busy: false) == .wait(until: 4))
        #expect(l.lastSaid.isEmpty)
    }

    @Test func oldLinesAreForgotten() {
        var l = CueLimiter()
        l.spoke("A", at: 0)
        l.spoke("B", at: 11)
        #expect(Set(l.lastSaid.keys) == ["B"])
    }
}

struct VoiceCueMappingTests {
    @Test func importantCuesAreSpokenBriefly() {
        #expect(VoiceCues.line(for: "Move closer here") == "Move closer here.")
        #expect(VoiceCues.line(for: "Other side") == "Now the other side.")
        #expect(VoiceCues.line(for: "Keep the object in frame") == "Keep it in frame.")
        #expect(VoiceCues.line(for: "All sides covered") == "All sides covered. Tap Done.")
        #expect(VoiceCues.line(for: "Scan saved") == "Got it.")
        for line in VoiceCues.spoken.values { #expect(line.split(separator: " ").count <= 6, "\(line)") }
    }

    @Test func steadyCuesStaySilent() {
        for cue in ["Walk slowly around it", "Keep walking around it", "Hold still a moment", "Move slowly",
                    "Point at the object", "Finding the object…", nil] {
            #expect(VoiceCues.line(for: cue) == nil)
        }
    }

    /// Every spoken cue is one the capture flow really shows (its style table, the dome's "Other side", or the
    /// scan screen's own "All sides covered" / "Scan saved").
    @Test func spokenCuesAreRealCues() {
        // "Move closer" is the prefix of QualityGate's "Move closer: about N cm away" (said by its first part).
        let shown = Set(CueStyle.table.keys).union(["Other side", "All sides covered", "Scan saved", "Move closer"])
        for cue in VoiceCues.spoken.keys { #expect(shown.contains(cue), "\(cue) is never shown") }
    }

    @Test func fixedLinesAreUnique() {
        #expect(VoiceCues.fixedLines.count == Set(VoiceCues.spoken.values).count)
    }
}

struct ReportSpeechTests {
    static func report(_ verdict: String, safety: String? = nil, extra: [String: Any] = [:]) -> ReportModel {
        var r: [String: Any] = ["verdict": verdict,
                                "findings": [["finding_id": "f1", "part_class": "housing", "issue": "cracked",
                                              "part_words": "left corner near the hinge"]],
                                "most_likely": ["f1"]]
        if let safety { r["safety"] = [["code": "unplug_first", "text": safety]] }
        return ReportModel(json: r.merging(extra) { $1 })
    }

    @Test func verdictTopProblemThenSafety() {
        let s = ReportSpeech.lines(Self.report("potential_issues", safety: "Unplug it before opening. Then wait."))
        #expect(s == "I found likely problems. Most likely, the housing looks cracked, left corner near the hinge. "
                + "Safety first: unplug it before opening.")
    }

    @Test func looksGoodAndCantTell() {
        #expect(ReportSpeech.lines(Self.report("looks_good")) == "I didn't see any problems in these photos.")
        let r = Self.report("insufficient_evidence", extra: ["verdict_reason": ["text": "The photos are too blurry. More."]])
        #expect(ReportSpeech.lines(r) == "I couldn't tell from these photos. The photos are too blurry.")
    }

    @Test func noProblemsSeenIsNotAllClear() {
        let r = Self.report("no_problems_seen")
        // Never "it's fine"; the caveat is not said on landing (owner, 2026-10-25: no coverage caveat by default).
        #expect(ReportSpeech.lines(r, caveat: "The inside was not photographed. More.")
                == "Nothing looked wrong in what I could see clearly.")
    }

    @Test func acronymsKeepTheirCase() {
        #expect(ReportSpeech.lowerFirst("USB socket") == "USB socket")
        #expect(ReportSpeech.lowerFirst("Housing") == "housing")
    }
}

struct PCMWAVTests {
    @Test func samplesOutOfAWAV() {
        var wav = Data("RIFF".utf8) + Data([0, 0, 0, 0]) + Data("WAVE".utf8)
        wav += Data("fmt ".utf8) + Data([16, 0, 0, 0]) + Data(count: 16)
        let pcm = Data([1, 2, 3, 4, 5, 6])
        wav += Data("data".utf8) + Data([6, 0, 0, 0]) + pcm
        #expect(PCMPlayer.pcm(fromWAV: wav) == pcm)
        #expect(PCMPlayer.pcm(fromWAV: Data("nope".utf8)) == nil)
    }
}

@MainActor
struct WakePhraseTests {
    @Test func tolerantMatching() {
        #expect(WakeListener.afterWake("Hey Lea") == "")
        #expect(WakeListener.afterWake("hey leah, is it safe to open?") == "is it safe to open")
        #expect(WakeListener.afterWake("Hi Lia what's wrong with it") == "what's wrong with it")
        #expect(WakeListener.afterWake("okay so hey Leia it's broken") == "it's broken")
    }

    @Test func otherSpeechIsIgnored() {
        #expect(WakeListener.afterWake("they leave it on the table") == nil)
        #expect(WakeListener.afterWake("hello there") == nil)
        #expect(WakeListener.afterWake("") == nil)
    }
}

/// Answers to Lea's own question: a plain yes takes the offer on screen, and a one-word yes or no is a turn even in
/// the follow-up window (where a single stray word is not).
@MainActor
struct ShortAnswerTests {
    @Test func aPlainYesTakesTheOffer() {
        for yes in ["Yes.", "yeah", "Yeah, let's do it", "okay go ahead", "sure please", "yes Lea"] {
            #expect(LeaConversation.isYes(yes), "\(yes)")
        }
        for not in ["yes but it smells burnt", "no", "what would the scan show?", "maybe later"] {
            #expect(!LeaConversation.isYes(not), "\(not)")
        }
    }

    @Test func oneWordAnswersAreTurns() {
        #expect(WakeListener.isShortAnswer("Yes."))
        #expect(WakeListener.isShortAnswer("nope"))
        #expect(!WakeListener.isShortAnswer("um"))
        #expect(!WakeListener.isShortAnswer("television"))
    }
}

@MainActor
struct RealtimeTests {
    @Test func wakePhraseIsDroppedFromInk2Transcripts() {
        #expect(WakeListener.dropWake("Hey Leah, is it safe to open my charger?") == "is it safe to open my charger?")
        #expect(WakeListener.dropWake("It stopped after I dropped it.") == "It stopped after I dropped it.")
        #expect(WakeListener.dropWake("Hey Lea") == "")
    }

    @Test func eagerAndFinalWordsMatchLoosely() {
        #expect(LeaConversation.same("Is it safe to open?", "is it safe to open"))
        #expect(!LeaConversation.same("Is it safe to open", "Is it safe to open it up"))
    }

    @Test func micAudioBecomes16kHzChunksWithBackfill() {
        let feed = PCM16Feed()
        let fmt = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 48000, channels: 1, interleaved: false)!
        for _ in 0..<10 {   // 10 × 4800 frames = 1 s at 48 kHz
            let b = AVAudioPCMBuffer(pcmFormat: fmt, frameCapacity: 4800)!
            b.frameLength = 4800
            for i in 0..<4800 { b.floatChannelData![0][i] = sin(Float(i) * 0.05) * 0.5 }
            feed.feed(b)
        }
        let got = Locked2()
        feed.open(backfill: 0.5) { got.add($0.count) }
        // 0.5 s of 16 kHz s16 = 16000 bytes, sent in 100 ms (3200-byte) pieces.
        #expect(abs(got.total - 16000) <= 64)
        #expect(got.sizes.dropLast().allSatisfy { $0 == 3200 })
        feed.close()
    }
}

private final class Locked2: @unchecked Sendable {
    private let lock = NSLock()
    private(set) var sizes: [Int] = []
    var total: Int { lock.withLock { sizes.reduce(0, +) } }
    func add(_ n: Int) { lock.withLock { sizes.append(n) } }
}

/// Greptile (PR #13): when Cartesia stops mid-answer, the sentence already sent with `audio: true` got no audio and
/// was never said. The phone's voice now says it, then the rest.
struct StreamFallbackTests {
    @Test func aSentenceWhoseAudioNeverCameIsSaidOnTheDevice() {
        var f = StreamFallback()
        #expect(f.sentence("The connector may be loose.", withAudio: true) == nil)
        #expect(f.audioOff() == "The connector may be loose.")
        #expect(f.audioOff() == nil)                                         // said once
        #expect(f.sentence("Unplug it first.", withAudio: false) == "Unplug it first.")
    }

    @Test func aSentenceWithItsAudioIsNotSaidTwice() {
        var f = StreamFallback()
        #expect(f.sentence("One.", withAudio: true) == nil)
        f.audio()
        #expect(f.sentence("Two.", withAudio: true) == nil)
        f.audio()
        #expect(f.audioOff() == nil)   // Cartesia stopped after Two's audio started: nothing is repeated
    }

    @Test func aOneSentenceAnswerIsNeverSilent() {
        var f = StreamFallback()
        _ = f.sentence("It looks fine from these photos.", withAudio: true)
        #expect(f.audioOff() == "It looks fine from these photos.")
    }
}

/// Lea's look-and-point (Voice/LeaSight): where a mark in the photo lands on the object.
@MainActor
struct LeaSightTests {
    @Test func photoAndSensorCoordinatesRoundTrip() {
        // The photo is the sensor image turned right: its top-left is the sensor's bottom-left.
        #expect(LeaSight.sensor(CGPoint(x: 0, y: 0)) == CGPoint(x: 0, y: 1))
        #expect(LeaSight.sensor(CGPoint(x: 0.5, y: 0.25)) == CGPoint(x: 0.25, y: 0.5))
        for p in [CGPoint(x: 0.1, y: 0.9), CGPoint(x: 0.42, y: 0.61)] {
            let back = LeaSight.portrait(LeaSight.sensor(p))
            #expect(abs(back.x - p.x) < 1e-9 && abs(back.y - p.y) < 1e-9)
        }
    }

    @Test func aMarkIsPinnedFromTheCameraAndDepthOfItsPhoto() {
        let k = simd_float3x3(SIMD3(1000, 0, 0), SIMD3(0, 1000, 0), SIMD3(960, 720, 1))
        var moved = matrix_identity_float4x4
        moved.columns.3 = SIMD4(1, 2, 3, 1)   // the phone 1 m right, 2 m up, 3 m back
        // A 4 × 3 depth map of the 1920 × 1440 image: intrinsics scaled as DepthCopy.from does.
        let sx: Float = 4.0 / 1920, sy: Float = 3.0 / 1440
        let depth = DepthCopy(width: 4, height: 3, depth: [Float](repeating: 0.5, count: 12), confidence: [UInt8](repeating: 2, count: 12),
                              fx: 1000 * sx, fy: 1000 * sy, cx: 960 * sx, cy: 720 * sy, cameraToWorld: moved, timestamp: 0)
        let look = LeaSight.Snapshot(transform: moved, intrinsics: k, resolution: CGSize(width: 1920, height: 1440), depth: depth)
        let centre = LeaSight.world(at: CGPoint(x: 0.5, y: 0.5), look, session: ARSession())
        #expect(centre.map { simd_distance($0, SIMD3(1, 2, 2.5)) < 0.001 } == true)   // 0.5 m straight ahead
        // A point right of centre in the sensor image lies right of the camera's axis.
        let right = LeaSight.world(at: CGPoint(x: 0.75, y: 0.5), look, session: ARSession())
        #expect(right.map { $0.x > 1.1 && abs($0.z - 2.5) < 0.001 } == true)
    }

    /// The person's pointer is drawn where the pointed spot is in the photo actually sent, wherever the phone is.
    @Test func thePointedSpotIsFoundAgainInALaterFrame() {
        let k = simd_float3x3(SIMD3(1000, 0, 0), SIMD3(0, 1000, 0), SIMD3(960, 720, 1))
        let res = CGSize(width: 1920, height: 1440)
        let spot = SIMD3<Float>(0.1, 0, -1)   // pointed at from the origin
        let there = LeaSight.sensorPoint(of: spot, transform: matrix_identity_float4x4, intrinsics: k, resolution: res)
        #expect(there.map { abs($0.x - 0.5521) < 0.001 && abs($0.y - 0.5) < 0.001 } == true)
        var moved = matrix_identity_float4x4
        moved.columns.3 = SIMD4(0.2, 0, 0, 1)   // the phone moved 20 cm right before the photo
        let later = LeaSight.sensorPoint(of: spot, transform: moved, intrinsics: k, resolution: res)
        #expect(later.map { $0.x < 0.5 } == true)   // the spot is now left of centre
        var turned = matrix_identity_float4x4
        turned.columns.3 = SIMD4(0, 0, -2, 1)       // walked past it: behind the camera, so no ring
        #expect(LeaSight.sensorPoint(of: spot, transform: turned, intrinsics: k, resolution: res) == nil)
    }

    @Test func depthOutsideTheUsefulRangeOrLowConfidenceIsNotUsed() {
        func one(_ metres: Float, _ conf: UInt8) -> DepthCopy {
            DepthCopy(width: 1, height: 1, depth: [metres], confidence: [conf], fx: 1, fy: 1, cx: 0.5, cy: 0.5,
                      cameraToWorld: matrix_identity_float4x4, timestamp: 0)
        }
        #expect(one(0.01, 2).worldPoint(at: CGPoint(x: 0.5, y: 0.5)) == nil)
        #expect(one(9, 2).worldPoint(at: CGPoint(x: 0.5, y: 0.5)) == nil)
        #expect(one(1.2, 0).worldPoint(at: CGPoint(x: 0.5, y: 0.5)) == nil)   // ARKit unsure of this pixel
        #expect(one(1.2, 2).worldPoint(at: CGPoint(x: 2, y: -1)).map { abs($0.z + 1.2) < 0.001 } == true)   // clamped
    }
}

/// What Lea is sent: photo size, the keyframe ring's rule, LiDAR words, measuring, and the converse query.
@MainActor
struct LeaLookingTests {
    /// A 1920 x 1440 sensor frame becomes a 576 x 768 portrait photo, whatever the screen scale (a screen-scale
    /// renderer made 2304 px).
    @Test func thePhotoIs768PixelsOnItsLongSideAtScaleOne() throws {
        let sensor = CIImage(color: CIColor(red: 0.4, green: 0.5, blue: 0.6)).cropped(to: CGRect(x: 0, y: 0, width: 1920, height: 1440))
        let jpeg = try #require(LeaSight.jpeg(sensor, ring: CGPoint(x: 0.5, y: 0.5)))
        let cg = try #require(UIImage(data: jpeg)?.cgImage)
        #expect(cg.width == 576 && cg.height == 768)
        #expect(jpeg.count < 150_000)
    }

    @Test func aKeyframeIsSharpNewAndAtMostOneEveryTwoSeconds() {
        let t0 = Date()
        var near = matrix_identity_float4x4
        near.columns.3 = SIMD4(0.03, 0, 0, 1)    // 3 cm: not a new view
        var far = matrix_identity_float4x4
        far.columns.3 = SIMD4(0.09, 0, 0, 1)     // 9 cm: a new view
        let turned = simd_float4x4(simd_quatf(angle: 12 * .pi / 180, axis: SIMD3(0, 1, 0)))   // 12 degrees
        let last = (pose: matrix_identity_float4x4, at: t0)
        #expect(KeyframeRule.accept(last: nil, pose: far, at: t0, sharpness: 100, medianSharpness: 100))
        #expect(!KeyframeRule.accept(last: last, pose: far, at: t0.addingTimeInterval(1.5), sharpness: 100, medianSharpness: 100))
        #expect(KeyframeRule.accept(last: last, pose: far, at: t0.addingTimeInterval(2.1), sharpness: 100, medianSharpness: 100))
        #expect(KeyframeRule.accept(last: last, pose: turned, at: t0.addingTimeInterval(2.1), sharpness: 100, medianSharpness: 100))
        #expect(!KeyframeRule.accept(last: last, pose: near, at: t0.addingTimeInterval(5), sharpness: 100, medianSharpness: 100))
        #expect(!KeyframeRule.accept(last: last, pose: far, at: t0.addingTimeInterval(5), sharpness: 50, medianSharpness: 100))   // blurry
    }

    @Test func lidarFactsAreWordsLeaRepeats() {
        #expect(ScanSession.boxWords(SIMD3(0.208, 0.076, 0.410)) == "208 x 410 mm footprint, 76 mm tall")
        #expect(LeaSight.millimetres(SIMD3(0, 0, 0), SIMD3(0.03, 0.04, 0)) == 50)
    }

    @Test func measuringNeedsDepthAndNeverGuesses() {
        let sight = LeaSight()
        #expect(sight.measure(CGPoint(x: 0.2, y: 0.5), CGPoint(x: 0.8, y: 0.5)).contains("can't measure"))
    }

    @Test func noPhotosAreTakenOutsideAConversationWindow() async {
        let sight = LeaSight()
        sight.uploader = { _ in "blob://sha256:" + String(repeating: "a", count: 64) }
        #expect(!sight.looking)
        #expect(await sight.photosForTurn(nil) == nil)          // no frame, nothing kept
        sight.turnStarted(nil)
        #expect(sight.looking)
        sight.closeWindow()
        #expect(!sight.looking)
    }

    @Test func aTurnSendsUpToThreePhotosOldestFirst() {
        let gateway = GatewayClient(baseURL: URL(string: "http://127.0.0.1:9")!, token: nil)
        let refs = (1...4).map { "blob://sha256:" + String(repeating: "\($0)", count: 64) }
        let q = gateway.converseRequest(scanID: "scan_1", audio: nil, text: "what is this", speech: false, images: refs).url?.query ?? ""
        let sent = q.components(separatedBy: "&").filter { $0.hasPrefix("image=") }
        #expect(sent.count == 3 && sent.last?.contains(String(repeating: "4", count: 64)) == true)
        #expect(sent.first?.contains(String(repeating: "2", count: 64)) == true)
    }

    @Test func aMeasureEventCarriesTwoPointsOfThePhoto() throws {
        let json = #"{"type":"measure","x1":0.1,"y1":0.2,"x2":0.3,"y2":0.4}"#
        guard case .measure(let a, let b)? = GatewayClient.event(UInt8(ascii: "J"), Data(json.utf8)) else {
            Issue.record("not a measure event")
            return
        }
        #expect(a == CGPoint(x: 0.1, y: 0.2) && b == CGPoint(x: 0.3, y: 0.4))
    }
}

/// Sonic failing part-way: the sentences the person has not heard are said by the phone (Greptile, PR #13).
struct SonicFailoverTests {
    let sent = ["The fan looks loose.", "Unplug it first.", "Then tighten the two screws."]

    @Test func nothingHeardMeansEverythingIsSaid() {
        #expect(LeaConversation.unheard(sent, heardSeconds: 0) == sent)
    }

    @Test func theCutOffSentenceIsSaidAgainWhole() {
        // "The fan looks loose." is about 1.5 s; 2 s in is part-way through the second sentence.
        #expect(LeaConversation.unheard(sent, heardSeconds: 2) == Array(sent.dropFirst()))
    }

    @Test func allHeardMeansNothingRepeats() {
        #expect(LeaConversation.unheard(sent, heardSeconds: 60).isEmpty)
    }
}

/// An early answer is asked for as held, so the gateway keeps it out of the conversation until it is committed
/// (Greptile, PR #21).
struct HeldAnswerTests {
    let gateway = GatewayClient(baseURL: URL(string: "http://127.0.0.1:9")!, token: nil)

    @Test func onlyAnEarlyAnswerIsHeld() {
        let held = gateway.converseRequest(scanID: "scan_1", audio: nil, text: "is it the fuse", speech: false, held: true)
        #expect(held.url?.query?.contains("held=1") == true)
        let final = gateway.converseRequest(scanID: "scan_1", audio: nil, text: "is it the fuse", speech: false)
        #expect(final.url?.query?.contains("held") == false)
    }
}
