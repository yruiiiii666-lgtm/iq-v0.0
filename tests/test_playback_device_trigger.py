from __future__ import annotations

import unittest

from playback_device import VisaPlaybackSession


class LatchingIQR:
    """Small IQR state model whose arm latch survives STOP like FW 04.10.x."""

    def __init__(self) -> None:
        self.state = "Running"
        self.armed = True
        self.mode = "SINGle"
        self.commands: list[str] = []

    def write(self, command: str) -> None:
        self.commands.append(command)
        if command == "TRIGger:PLAYer:STOP":
            self.state = "Ready"
        elif command == "TRIGger:PLAYer:ARM OFF":
            self.armed = False
            self.state = "Ready"
        elif command == "TRIGger:PLAYer:ARM ON":
            # The new trigger cycle exists only after a real OFF -> ON edge.
            if not self.armed:
                self.armed = True
                self.state = "Waiting for LAN remote trigger command"
        elif command == "TRIGger:PLAYer:MODE CONTinuous":
            self.mode = "CONTinuous"
        elif command == "TRIGger:PLAYer:MODE SINGle":
            self.mode = "SINGle"
        elif command == "TRIGger:PLAYer:EXECute":
            if self.armed and self.state == "Waiting for LAN remote trigger command":
                self.state = "Running"

    def query(self, command: str) -> str:
        self.commands.append(command)
        if command == "TRIGger:PLAYer:STATe?":
            return f'"{self.state}"'
        if command == "TRIGger:PLAYer:MODE?":
            return self.mode
        if command == "OUTPut:SYSTem:INSTrument:DESTination:IDENtification?":
            return "SMBV100A"
        if command == "OUTPut1:SYSTem:INSTrument:DESTination:STATus?":
            return "1"
        if command == "SYSTem:ERRor?":
            return '0,"No error"'
        raise AssertionError(f"unexpected query: {command}")


class IQRTriggerResetTests(unittest.TestCase):
    def test_loading_next_recording_resets_stale_arm_latch_before_rearming(self) -> None:
        session = VisaPlaybackSession()
        iqr = LatchingIQR()
        session.iqr = iqr

        status = session.load_iqr_recording("e:/second", continuous=False)

        self.assertIn("Waiting for LAN remote trigger command", status)
        stop_index = iqr.commands.index("TRIGger:PLAYer:STOP")
        arm_off_index = iqr.commands.index("TRIGger:PLAYer:ARM OFF")
        select_index = iqr.commands.index("OUTPut:PLAYer:WAVeform:SELect 'e:/second'")
        arm_on_index = iqr.commands.index("TRIGger:PLAYer:ARM ON")
        self.assertLess(stop_index, arm_off_index)
        self.assertLess(arm_off_index, select_index)
        self.assertLess(select_index, arm_on_index)

    def test_next_recording_executes_after_clean_off_on_arm_cycle(self) -> None:
        session = VisaPlaybackSession()
        iqr = LatchingIQR()
        session.iqr = iqr

        session.load_iqr_recording("e:/second", continuous=False)
        session.start(use_iqr=True, iqr_display_mode="IQ")

        self.assertEqual(iqr.state, "Running")
        self.assertEqual(iqr.commands[-1], "TRIGger:PLAYer:EXECute")
        self.assertEqual(iqr.commands.count("MEASure:SPECtrum:PLAYer:MODE IQ"), 1)

    def test_every_item_in_sequence_gets_a_fresh_trigger_cycle(self) -> None:
        session = VisaPlaybackSession()
        iqr = LatchingIQR()
        session.iqr = iqr

        for waveform in ("e:/second", "e:/third", "e:/fourth"):
            session.load_iqr_recording(waveform, continuous=False)
            session.start(use_iqr=True, iqr_display_mode="IQ")
            self.assertEqual(iqr.state, "Running")

        self.assertEqual(iqr.commands.count("TRIGger:PLAYer:ARM OFF"), 3)
        self.assertEqual(iqr.commands.count("TRIGger:PLAYer:ARM ON"), 3)
        self.assertEqual(iqr.commands.count("TRIGger:PLAYer:EXECute"), 3)


if __name__ == "__main__":
    unittest.main()
