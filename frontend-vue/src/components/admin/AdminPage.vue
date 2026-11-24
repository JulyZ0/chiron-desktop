<script setup lang="ts">
defineProps<{
  title?: string
  description?: string
}>()
</script>

<template>
  <section class="admin-page">
    <header
      v-if="title || description || $slots.actions"
      class="admin-page-header"
    >
      <div class="admin-page-heading">
        <slot name="title">
          <h1
            v-if="title"
            class="admin-page-title"
          >
            {{ title }}
          </h1>
        </slot>
        <slot name="description">
          <p
            v-if="description"
            class="admin-page-description"
          >
            {{ description }}
          </p>
        </slot>
      </div>
      <div
        v-if="$slots.actions"
        class="admin-page-actions"
      >
        <slot name="actions" />
      </div>
    </header>
    <div class="admin-page-body">
      <slot />
    </div>
  </section>
</template>
