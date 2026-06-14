import unittest

import voice_core.runtime as runtime


class PromptSoundRoutingTests(unittest.TestCase):
    def test_python_runtime_has_no_prompt_sound_api(self):
        for name in [
            "sound_start",
            "sound_done",
            "sound_toggle_on",
            "sound_toggle_off",
            "sound_error",
            "set_prompt_sounds_enabled",
            "prompt_sounds_enabled",
        ]:
            self.assertFalse(hasattr(runtime, name), name)


if __name__ == "__main__":
    unittest.main()
