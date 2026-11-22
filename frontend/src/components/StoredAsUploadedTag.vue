<script setup lang="ts">
/**
 * The mark on a study stored as uploaded: a warning tag and a tooltip saying what was done to its header, shown
 * wherever such a study can be opened or shared onward so that its owner does not hand it to readers by accident.
 * It states what the platform did and makes no statement about a study the header pass did run on.
 */
import { t } from '#i18n'

const SCOPE = 'StoredAsUploadedTag'

defineProps<{
    /** An element id unique on the page, which anchors the tooltip to this tag. */
    anchor: string
}>()
</script>

<template>
    <wa-tag
        class="stored-as-uploaded"
        :id="anchor"
        size="s"
        variant="warning"
    >
        {{ t('Stored as uploaded', SCOPE) }}
    </wa-tag>
    <wa-tooltip :for="anchor">
        {{
            t(
                'This study was stored without the platform\'s header pass. Its header carries the uploaded patient '
                    + 'fields.',
                SCOPE
            )
        }}
    </wa-tooltip>
</template>

<style scoped>
.stored-as-uploaded {
    flex-shrink: 0;
}
</style>
