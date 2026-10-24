// VEL's speech.test.mjs cases (turn helpers, sentences, resumeLine, reactions, stripLead) run against the Swift port,
// plus the endpoint.rs turn_hint rules. Run: swiftc ../../../MyApp/Voice/TurnTaking.swift main.swift -o tt && ./tt
import Foundation

nonisolated(unsafe) var failures = 0
func check(_ ok: Bool, _ what: String) { if !ok { failures += 1; print("FAIL \(what)") } }
typealias T = TurnTaking

// sentences
check(T.sentences("See this red line here? That's demand for coffee. When the price goes up, people buy less.") ==
      ["See this red line here?", "That's demand for coffee.", "When the price goes up, people buy less."], "sentences 1")
check(T.sentences("Okay. Now look at the second triangle here.") == ["Okay. Now look at the second triangle here."], "sentences 2")
check(T.sentences("a line with no stop") == ["a line with no stop"], "sentences 3")

// turn helpers
for t in ["ok back", "go on", "Okay, got it. Go on.", "continue please", "got it", "back to the lesson"] { check(T.isBack(t), "isBack \(t)") }
for t in ["what is back propagation", "why does it go on forever", "yes"] { check(!T.isBack(t), "!isBack \(t)") }
check(T.isBack("yeah", offered: true), "bare yes when offered")
check(T.isBackchannel("Mm-hmm."), "backchannel")
check(!T.isBackchannel("why is that"), "!backchannel")
check(T.isEcho(heard: "here is the magic nine plus sixteen", spoken: "Here's the magic. Nine plus sixteen is twenty-five. Here is the"), "echo")
check(!T.isEcho(heard: "wait why is that true", spoken: "Here's the magic. Nine plus sixteen is twenty-five."), "!echo")

// resumeLine
let s = "Nine plus sixteen is twenty-five, so the two small squares fill the big one exactly."
let r2 = T.resumeLine(s, heard: 0.6, n: 2).text
check(r2.range(of: "^(Right, |Okay, )?so the two small squares fill the big one exactly\\.$", options: .regularExpression) != nil, "resume clause: \(r2)")
check(T.resumeLine(s, heard: 0.6, n: 0).from == 5, "resume from 5")
check(T.resumeLine(s, heard: 0.1, n: 0).from == 0, "little heard")
check(T.resumeLine("The side across from it is the longest, the hypotenuse.", heard: 0.85, n: 2).from == 0, "never a scrap")
let links = Set((0...5).map { T.resumeLine("Every right triangle has one square corner.", heard: 0, n: $0).text })
check(links.count >= 4, "varied links: \(links)")
check(links.contains("Every right triangle has one square corner."), "sometimes no link")

// turnKind and reactions
for (t, k) in [("Can you explain the Pythagorean theorem?", T.Kind.request), ("Hmm, can you explain it in a simpler way?", .request),
               ("Wait, why is the hypotenuse always the longest side?", .question), ("Sorry, what does squared mean here?", .question),
               ("Okay, got it. Go on.", .back), ("I don't get it", .confused), ("cool", .statement), ("", .none)] {
    check(T.turnKind(t) == k, "turnKind \(t) = \(T.turnKind(t))")
}
check(T.reaction(for: "Okay, go on.", recent: [], r: 0.9) == nil, "no reaction to go on")
check(T.reactions[.request]!.contains(T.reaction(for: "Can you make it simpler?", recent: [], r: 0.9) ?? ""), "request reaction")
check(T.reaction(for: "why does it work?", recent: [], r: 0.1) == nil, "sometimes just a pause")
var said: [String] = []
for i in 0..<40 { if let x = T.reaction(for: "why does it work?", recent: said, r: (Double(i) * 0.37 + 0.25).truncatingRemainder(dividingBy: 1)) { said.append(x) } }
check(said.filter { $0.range(of: "good (question|one)", options: [.regularExpression, .caseInsensitive]) != nil }.count <= 1, "praise once")
for i in 3..<said.count { check(!said[(i - 3)..<i].contains(said[i]), "never one of the last three") }

// stripLead
check(T.stripLead("Ah, Pythagoras.") == "Pythagoras.", "stripLead 1")
check(T.stripLead("Okay, this is a fun one.") == "This is a fun one.", "stripLead 2")
check(T.stripLead("Sure. Think of it as a shortcut.") == "Think of it as a shortcut.", "stripLead 3")
check(T.stripLead("Pythagoras was Greek.") == "Pythagoras was Greek.", "stripLead 4")
check(T.stripLead("Oh.") == "Oh.", "stripLead 5")

// end of turn (endpoint.rs)
check(T.turnHint("so I measured the fan and") == .unfinished, "hint: hanging word")
check(T.turnHint("the voltage is,") == .unfinished, "hint: comma")
check(T.turnHint("What did you find?") == .finished, "hint: question")
check(T.turnHint("hello") == .unsure, "hint: short")
check(T.hangover(after: "What did you find?") == 0.576 && T.hangover(after: "the um") == 1.8 && T.hangover(after: "hello") == 0.9, "hangovers")

// Lea additions
for t in ["stop", "Wait.", "hold on a second", "okay stop"] { check(T.isStop(t), "isStop \(t)") }
check(!T.isStop("stop the motor first"), "!isStop instruction")
check(T.turnKind("Can you check the connector?") == .request, "lea request")
check(T.speakable("Widen it by 2 mm, reads 4.9 V") == "Widen it by 2 millimetres, reads 4.9 volts", "speakable units: \(T.speakable("Widen it by 2 mm, reads 4.9 V"))")

print(failures == 0 ? "ALL PASS" : "\(failures) FAILED")
exit(failures == 0 ? 0 : 1)
