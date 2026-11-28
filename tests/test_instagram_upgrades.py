import asyncio
import datetime as dt
import json
import os
import tempfile
import unittest
from unittest import mock
import zoneinfo

os.environ.setdefault("TARGET_CHAT_ID", "test-chat")
os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("GEMINI_API_KEY", "test-key")
zoneinfo.ZoneInfo = lambda _name: dt.timezone.utc

import daily_news
import insta_slides
import video


class InstagramUpgradeTests(unittest.TestCase):
    def test_recent_headline_file_prunes_old_and_caps_to_40(self):
        day = dt.date(2026, 11, 28)
        with tempfile.TemporaryDirectory() as work:
            path = os.path.join(work, "slides", "recent_headlines.json")
            os.makedirs(os.path.dirname(path))
            records = [
                {"date": "2026-11-24", "headline": "Too old"},
                {"date": "2026-11-26", "headline": "Three days old"},
            ]
            records.extend(
                {"date": "2026-11-27", "headline": f"Headline {index:02d}"}
                for index in range(45)
            )
            with open(path, "w", encoding="utf-8") as file:
                json.dump(records, file)
            with mock.patch.object(insta_slides, "RECENT_HEADLINES_PATH", path):
                self.assertEqual(len(insta_slides._load_recent_headlines(day)), 40)
                self.assertEqual(
                    insta_slides._load_recent_headlines(day)[-1],
                    "Headline 44",
                )
                insta_slides._record_recent_headlines(
                    {"slides": [{"headline": "A **NEW STORY**"}]}, day)
                updated = insta_slides._recent_headline_records(day)

        self.assertEqual(len(updated), 40)
        self.assertEqual(updated[-1]["headline"], "A NEW STORY")
        self.assertTrue(all(record["date"] != "2026-11-24"
                            for record in updated))

    def test_write_deck_includes_recent_headlines_in_editorial_prompt(self):
        response = mock.Mock(
            ok=True,
            status_code=200,
            json=mock.Mock(return_value={
                "candidates": [{
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": "{}"}]},
                }],
            }),
        )
        with (
            mock.patch.object(insta_slides, "_text_model_attempts",
                              return_value=("mock-model",)),
            mock.patch.object(insta_slides.dn, "_photo_parts", return_value=[]),
            mock.patch.object(insta_slides.dn, "_extract_json", return_value={}),
            mock.patch.object(insta_slides, "_validate_generated_deck",
                              return_value={"ok": True}),
            mock.patch.object(insta_slides.requests, "post",
                              return_value=response) as post,
        ):
            result = insta_slides.write_deck(
                [], dt.date(2026, 11, 28), wire="", emotion_items=[],
                recent_headlines=["THE SAME EVENT FROM YESTERDAY"],
            )

        prompt = post.call_args.kwargs["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn("THE SAME EVENT FROM YESTERDAY", prompt)
        self.assertIn("major new factual development", prompt)
        self.assertEqual(result, {"ok": True})

    def test_schedule_gate_matches_cron_to_beirut_dst_offset(self):
        workflow = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            ".github", "workflows", "instagram.yml")
        with open(workflow, encoding="utf-8") as file:
            contents = file.read()
        self.assertIn('[ "$OFFSET" = "+0200" ]', contents)
        self.assertIn('[ "$SCHEDULE" = "15 22 * * *" ]', contents)
        self.assertIn('[ "$OFFSET" = "+0300" ]', contents)
        self.assertIn('[ "$SCHEDULE" = "15 21 * * *" ]', contents)
        self.assertNotIn('$(date +%H)', contents)
        carousel_publish = contents.index("- name: Publish to Instagram")
        reel_render = contents.index("- name: Render optional Reel")
        reel_publish = contents.index("- name: Publish optional Reel")
        self.assertLess(carousel_publish, reel_render)
        self.assertLess(reel_render, reel_publish)

    def test_video_target_bitrate_matches_required_bounds(self):
        self.assertEqual(daily_news.target_video_bitrate(1), 2200)
        self.assertEqual(
            daily_news.target_video_bitrate(600),
            min(2200, max(750, int((46 * 8192) / 600) - 136)),
        )
        self.assertEqual(daily_news.target_video_bitrate(100000), 750)
        with mock.patch.object(video, "VIDEO_BITRATE_KBPS", None):
            video.set_video_bitrate(1234)
            args = video._encoding_args()
            self.assertIn("-b:v", args)
            self.assertIn("1234k", args)
            self.assertNotIn("-crf", args)
        video.VIDEO_BITRATE_KBPS = None

    def test_reel_renders_three_4k_motion_clips_and_static_outro(self):
        with tempfile.TemporaryDirectory() as work:
            scenes = os.path.join(work, "reel_scenes")
            os.makedirs(scenes)
            slides = [
                {"headline": f"STORY {index} **HEADLINE**",
                 "caption": "First factual sentence. Second factual sentence.",
                 "scene_prompt": f"Photorealistic scene {index}"}
                for index in range(1, 4)
            ]
            with open(os.path.join(work, "deck.json"), "w",
                      encoding="utf-8") as file:
                json.dump({"date": "2026-11-28", "slides": slides}, file)
            for index in range(1, 4):
                from PIL import Image
                Image.new("RGB", (1080, 1920), "navy").save(
                    os.path.join(scenes, f"{index:02d}.jpg"))

            commands = []

            def fake_run(command):
                commands.append(command)
                output_path = command[-1]
                with open(output_path, "wb") as file:
                    file.write(b"x" * 2048)

            async def fake_speak(_text, path):
                with open(path, "wb") as file:
                    file.write(b"audio")

            with (
                mock.patch.object(insta_slides, "OUT_DIR", work),
                mock.patch.object(daily_news, "_speak", side_effect=fake_speak),
                mock.patch.object(video, "probe_duration",
                                  side_effect=[30, 30, 30, 63.5]),
                mock.patch.object(video, "run", side_effect=fake_run),
            ):
                output = insta_slides.render_reel()

            self.assertTrue(os.path.isfile(output))
            self.assertLess(os.path.getsize(output), 25 * 1024 * 1024)
            story_commands = [
                command for command in commands
                if any("reel_story_" in argument for argument in command)
            ]
            self.assertEqual(len(story_commands), 3)
            for command in story_commands:
                graph = command[command.index("-vf") + 1]
                self.assertIn("scale=2160:3840", graph)
                self.assertIn("zoompan=", graph)
                self.assertIn("drawtext=", graph)
                self.assertIn("atempo", command[command.index("-af") + 1])

    def test_publish_reel_waits_and_publishes_to_feed(self):
        with tempfile.TemporaryDirectory() as work:
            with open(os.path.join(work, "deck.json"), "w",
                      encoding="utf-8") as file:
                json.dump({
                    "date": "2026-11-28",
                    "slides": [
                        {"caption": "Story caption."},
                        {"caption": "Another story."},
                        {"caption": "Third story."},
                    ],
                }, file)
            reel = os.path.join(work, "reel.mp4")
            with open(reel, "wb") as file:
                file.write(b"reel")

            def response(payload):
                result = __import__("requests").Response()
                result.status_code = 200
                result._content = json.dumps(payload).encode()
                return result

            with (
                mock.patch.object(insta_slides, "OUT_DIR", work),
                mock.patch.dict(os.environ, {
                    "IG_ACCESS_TOKEN": "test-token",
                    "IG_USER_ID": "123",
                    "GITHUB_REPOSITORY": "owner/repo",
                }),
                mock.patch.object(insta_slides.requests, "post",
                                  side_effect=[
                                      response({"id": "container-1"}),
                                      response({"id": "published-1"}),
                                  ]) as post,
                mock.patch.object(insta_slides, "_wait_for_container") as wait,
            ):
                insta_slides.publish_reel()

            data = post.call_args_list[0].kwargs["data"]
            self.assertEqual(data["media_type"], "REELS")
            self.assertTrue(data["video_url"].endswith("/reel.mp4"))
            self.assertEqual(data["share_to_feed"], "true")
            wait.assert_called_once_with(
                "container-1", "test-token", insta_slides.graph_base("test-token"),
                attempts=25, sleep_seconds=4)
            with open(os.path.join(work, ".last_ig_reel"),
                      encoding="utf-8") as file:
                self.assertEqual(file.read(), "2026-11-28")

    def test_reel_publish_failure_is_non_fatal(self):
        with tempfile.TemporaryDirectory() as work:
            with open(os.path.join(work, "deck.json"), "w",
                      encoding="utf-8") as file:
                json.dump({"date": "2026-11-28", "slides": []}, file)
            with open(os.path.join(work, "reel.mp4"), "wb") as file:
                file.write(b"reel")
            with (
                mock.patch.object(insta_slides, "OUT_DIR", work),
                mock.patch.dict(os.environ, {
                    "IG_ACCESS_TOKEN": "test-token",
                    "IG_USER_ID": "123",
                }),
                mock.patch.object(insta_slides.requests, "post",
                                  side_effect=RuntimeError("test failure")),
            ):
                insta_slides.publish_reel()

    def test_anchor_talk_trims_voice_lead_in_before_sync(self):
        with tempfile.TemporaryDirectory() as work:
            calls = []

            def fake_run(command):
                calls.append(command)

            with (
                mock.patch.object(video, "probe_duration", return_value=8.0),
                mock.patch.object(video, "trim_leading_silence",
                                  return_value=os.path.join(work, "trim.mp3")) as trim,
                mock.patch.object(video, "_speech_onset", side_effect=[0.8, 0.0]),
                mock.patch.object(video, "run", side_effect=fake_run),
                mock.patch.object(video, "VIDEO_BITRATE_KBPS", 900),
            ):
                video.render_anchor_talk(
                    work, "anchor.mp4", "voice.mp3", "MIDWORLD DAILY",
                    "28 November 2026", os.path.join(work, "talk.mp4"))

            trim.assert_called_once_with(
                "voice.mp3", os.path.join(work, "talk_leadtrim.mp3"))
            self.assertEqual(len(calls), 1)
            audio_graph = calls[0][calls[0].index("-filter_complex") + 1]
            self.assertIn("adelay=800", audio_graph)
            self.assertIn("900k", calls[0])


if __name__ == "__main__":
    unittest.main()
