-- Duplicates (A10, Decision 9): a channel message id is stored at most once.
-- The supplied rows have unique external ids (THR009's retry carries a different id,
-- so intake also dedupes on driver + text + 2-minute bucket before this point).
CREATE UNIQUE INDEX ux_chat_external_message_id ON chat_messages(external_message_id)
  WHERE external_message_id IS NOT NULL;
