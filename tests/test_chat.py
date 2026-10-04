"""Channel chat: dedupe, quotes and reaction pills (``meshgraph/chat.py``)."""

from __future__ import annotations

import time

from meshgraph import chat, store

from .conftest import make_packet

NOW = time.time()


def add_text(
    settings,
    text: str,
    *,
    packet_id: int | None = None,
    from_node: int = 1,
    ts: float | None = None,
    reply_id: int | None = None,
    emoji: int | None = None,
    to: int = 0xFFFFFFFF,
    gateway: int = 2,
):
    """Insert one broadcast TEXT_MESSAGE_APP packet the decoder would emit."""
    store.insert_packet(
        settings.db_file,
        make_packet(
            timestamp=ts,
            from_node_id=from_node,
            to_node_id=to,
            portnum_name="TEXT_MESSAGE_APP",
            gateway_node_id=gateway,
            raw_payload=text.encode("utf-8"),
            mesh_packet_id=packet_id,
            reply_id=reply_id,
            emoji=emoji,
        ),
    )


def add_node(settings, node_id: int, long_name: str):
    store.insert_packet(
        settings.db_file,
        make_packet(
            from_node_id=node_id,
            portnum_name="NODEINFO_APP",
            node_info={"node_id": node_id, "long_name": long_name},
        ),
    )


# ---------------------------------------------------------------------------
# Plain messages
# ---------------------------------------------------------------------------

def test_messages_come_back_in_time_order(settings):
    add_text(settings, "первое", packet_id=11, ts=NOW - 120)
    add_text(settings, "второе", packet_id=22, ts=NOW - 60)

    result = chat.build_chat(settings, hours=24)

    assert [m["text"] for m in result["messages"]] == ["первое", "второе"]
    assert result["messages"][0]["packet_id"] == 11
    assert result["messages"][0]["reply_to"] is None
    assert result["messages"][0]["reactions"] is None


def test_receptions_from_many_gateways_collapse_to_one(settings):
    add_text(settings, "привет", packet_id=33, ts=NOW - 100, gateway=7)
    add_text(settings, "привет", packet_id=33, ts=NOW - 90, gateway=8)

    messages = chat.build_chat(settings, hours=24)["messages"]

    assert len(messages) == 1
    assert messages[0]["ts"] == NOW - 100  # схлопнули к самому раннему приёму


def test_names_resolve_from_the_node_table(settings):
    add_node(settings, 42, "Маяк")
    add_text(settings, "всем привет", packet_id=44, from_node=42, ts=NOW - 30)

    message = chat.build_chat(settings, hours=24)["messages"][0]

    assert message["name"] == "Маяк"
    assert message["hex_id"] == "!0000002a"


def test_direct_messages_do_not_enter_the_chat(settings):
    add_text(settings, "это ЛС", packet_id=700, ts=NOW - 100, to=0x8888)
    add_text(settings, "а это канал", packet_id=701, ts=NOW - 50)

    texts = [m["text"] for m in chat.build_chat(settings, hours=24)["messages"]]

    assert texts == ["а это канал"]


def test_hour_window_hides_old_messages(settings):
    add_text(settings, "давно", packet_id=800, ts=NOW - 5 * 3600)
    add_text(settings, "сейчас", packet_id=801, ts=NOW - 60)

    texts = [m["text"] for m in chat.build_chat(settings, hours=1)["messages"]]

    assert texts == ["сейчас"]


def test_channel_filter_keeps_one_channel(settings):
    store.insert_packet(
        settings.db_file,
        make_packet(
            from_node_id=3,
            portnum_name="TEXT_MESSAGE_APP",
            channel_id="Alarm",
            mesh_packet_id=1,
            raw_payload="тревога".encode(),
            timestamp=NOW - 60,
        ),
    )
    add_text(settings, "основной канал", packet_id=2, ts=NOW - 60)

    long_fast = [
        m["text"]
        for m in chat.build_chat(settings, hours=24, channel="LongFast")["messages"]
    ]
    assert long_fast == ["основной канал"]


def test_control_characters_are_stripped_newlines_kept(settings):
    add_text(settings, "первая строка\x00\x07\nвторая", packet_id=900, ts=NOW - 60)

    text = chat.build_chat(settings, hours=24)["messages"][0]["text"]

    assert text == "первая строка\uFFFD\uFFFD\nвторая"


def test_limit_clamps_to_a_sane_range(settings):
    for i in range(5):
        add_text(settings, f"msg {i}", packet_id=950 + i, ts=NOW - 60 + i)

    assert len(chat.build_chat(settings, hours=24, limit=2)["messages"]) == 2
    assert len(chat.build_chat(settings, hours=24, limit=0)["messages"]) == 5


def test_limit_counts_messages_not_receptions(settings):
    # У каждого сообщения три приёма (шесть шлюзов), но в чате это сообщения.
    for i in range(4):
        for gateway in (7, 8, 9):
            add_text(
                settings, f"m{i}", packet_id=970 + i, ts=NOW - 300 + i, gateway=gateway
            )

    messages = chat.build_chat(settings, hours=24, limit=4)["messages"]

    assert [m["text"] for m in messages] == ["m0", "m1", "m2", "m3"]


# ---------------------------------------------------------------------------
# Replies
# ---------------------------------------------------------------------------

def test_reply_shows_quote_of_the_target(settings):
    add_text(settings, "Кто на связи?", packet_id=100, from_node=5, ts=NOW - 200)
    add_text(settings, "Я!", packet_id=101, from_node=6, ts=NOW - 100, reply_id=100)

    reply = chat.build_chat(settings, hours=24)["messages"][1]

    assert reply["reply_to"] == {
        "packet_id": 100,
        "from": 5,
        "name": "!00000005",
        "text": "Кто на связи?",
        "ts": NOW - 200,
    }


def test_reply_pulls_target_from_outside_the_window(settings):
    add_text(settings, "утро", packet_id=200, ts=NOW - 3 * 3600)
    add_text(settings, "доброе", packet_id=201, ts=NOW - 60, reply_id=200)

    messages = chat.build_chat(settings, hours=1)["messages"]

    assert [m["text"] for m in messages] == ["утро", "доброе"]


def test_reply_to_a_pruned_target_keeps_a_stub(settings):
    add_text(settings, "ответ без цели", packet_id=301, ts=NOW - 60, reply_id=999999)

    message = chat.build_chat(settings, hours=24)["messages"][0]

    assert message["text"] == "ответ без цели"
    assert message["reply_to"] == {"packet_id": 999999}


# ---------------------------------------------------------------------------
# Reactions
# ---------------------------------------------------------------------------

def test_emoji_replies_become_a_pill_on_the_target(settings):
    add_text(settings, "Отличный день!", packet_id=400, ts=NOW - 300)
    add_text(settings, "👍", packet_id=401, from_node=7, ts=NOW - 200, reply_id=400)
    add_text(
        settings, "👍", packet_id=402, from_node=8, ts=NOW - 100, reply_id=400, emoji=1
    )

    messages = chat.build_chat(settings, hours=24)["messages"]

    assert len(messages) == 1  # сами реакции отдельными сообщениями не идут
    target = messages[0]
    assert target["text"] == "Отличный день!"
    assert target["reactions"] == [
        {"emoji": "👍", "count": 2, "names": ["!00000007", "!00000008"]}
    ]


def test_emoji_flag_marks_a_reaction_even_without_emoji_text(settings):
    add_text(settings, "Всё готово", packet_id=500, ts=NOW - 300)
    add_text(
        settings,
        "спасибо",
        packet_id=501,
        from_node=9,
        ts=NOW - 100,
        reply_id=500,
        emoji=1,
    )

    messages = chat.build_chat(settings, hours=24)["messages"]

    assert len(messages) == 1
    assert messages[0]["reactions"] == [
        {"emoji": "спасибо", "count": 1, "names": ["!00000009"]}
    ]


def test_same_node_repeating_a_reaction_counts_once(settings):
    add_text(settings, "встреча в 20:00", packet_id=600, ts=NOW - 300)
    add_text(settings, "🔥", packet_id=601, from_node=7, ts=NOW - 200, reply_id=600)
    add_text(settings, "🔥", packet_id=602, from_node=7, ts=NOW - 100, reply_id=600)

    reactions = chat.build_chat(settings, hours=24)["messages"][0]["reactions"]

    assert reactions == [{"emoji": "🔥", "count": 1, "names": ["!00000007"]}]


def test_reaction_to_an_unknown_target_builds_a_phantom(settings):
    add_text(
        settings, "👍", packet_id=701, ts=NOW - 60, reply_id=999999, emoji=1
    )

    messages = chat.build_chat(settings, hours=24)["messages"]

    assert len(messages) == 1
    phantom = messages[0]
    assert phantom["phantom"] is True
    assert phantom["packet_id"] == 999999
    assert phantom["id"] < 0  # не пересекается с id настоящих строк
    assert phantom["ts"] == NOW - 60  # момент первой реакции
    assert phantom["reactions"] == [
        {"emoji": "👍", "count": 1, "names": ["!00000001"]}
    ]


def test_many_reactions_to_one_missing_target_share_a_phantom(settings):
    add_text(
        settings, "👍", packet_id=711, from_node=7, ts=NOW - 90,
        reply_id=999999, emoji=1,
    )
    add_text(
        settings, "👍", packet_id=712, from_node=8, ts=NOW - 60,
        reply_id=999999, emoji=1,
    )
    add_text(
        settings, "🔥", packet_id=713, from_node=9, ts=NOW - 30,
        reply_id=999999, emoji=1,
    )

    messages = chat.build_chat(settings, hours=24)["messages"]

    assert len(messages) == 1  # не три «сообщения-ответа», а один фантом
    assert messages[0]["reactions"] == [
        {"emoji": "👍", "count": 2, "names": ["!00000007", "!00000008"]},
        {"emoji": "🔥", "count": 1, "names": ["!00000009"]},
    ]


def test_reply_keeps_its_stub_next_to_a_phantom(settings):
    add_text(settings, "ответ", packet_id=721, ts=NOW - 70, reply_id=999999)
    add_text(
        settings, "👍", packet_id=722, from_node=7, ts=NOW - 60,
        reply_id=999999, emoji=1,
    )

    messages = chat.build_chat(settings, hours=24)["messages"]

    assert len(messages) == 2  # текстовый ответ остаётся сообщением
    reply, phantom = messages
    assert reply["text"] == "ответ"
    assert reply["phantom"] is False
    assert reply["reply_to"] == {"packet_id": 999999}
    assert phantom["phantom"] is True
    assert phantom["reactions"][0]["emoji"] == "👍"


def test_standalone_emoji_stays_a_regular_message(settings):
    add_text(settings, "🤷🏻‍♂️", packet_id=801, ts=NOW - 60)

    message = chat.build_chat(settings, hours=24)["messages"][0]

    assert message["emoji_only"] is True
    assert message["reactions"] is None
    assert message["reply_to"] is None


def test_reaction_to_a_message_outside_the_window_pulls_it_in(settings):
    add_text(settings, "старая новость", packet_id=900, ts=NOW - 3 * 3600)
    add_text(settings, "🔥", packet_id=901, from_node=7, ts=NOW - 60, reply_id=900)

    messages = chat.build_chat(settings, hours=1)["messages"]

    assert [m["text"] for m in messages] == ["старая новость"]
    assert messages[0]["reactions"][0]["emoji"] == "🔥"


# ---------------------------------------------------------------------------
# Emoji detection (pure helper)
# ---------------------------------------------------------------------------

def test_is_emoji_only_recognises_sequences():
    assert chat.is_emoji_only("👍")
    assert chat.is_emoji_only("👍 🔥")
    assert chat.is_emoji_only("5️⃣")        # keycap: цифра внутри
    assert chat.is_emoji_only("🤷🏻‍♂️")     # skin tone + ZWJ
    assert chat.is_emoji_only("🇬🇧")


def test_is_emoji_only_rejects_ordinary_text():
    assert not chat.is_emoji_only("")
    assert not chat.is_emoji_only("   ")
    assert not chat.is_emoji_only("5")
    assert not chat.is_emoji_only("привет 👍")
    assert not chat.is_emoji_only("👍 ok")
    assert not chat.is_emoji_only("10/10")
