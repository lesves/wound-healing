<script setup>
    const emit = defineEmits(["keyup"])

    function keyup(event) {
        emit("keyup", event);
    }

    window.addEventListener("keyup", keyup);
</script>

<template>
    <div></div>
</template>
