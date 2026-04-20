import os
from common.defines import (
    TAPPAS_POSTPROC_PATH_KEY,
    GST_VIDEO_SINK,
    TAPPAS_POSTPROC_PATH_DEFAULT,
)


def get_source_type(input_source):
    input_source = str(input_source)
    if input_source.startswith("/dev/video"):
        return "usb"
    elif input_source.startswith("rpi"):
        return "rpi"
    elif input_source.startswith("libcamera"):
        return "libcamera"
    elif input_source.startswith("rtsp"):
        return "rtsp"
    elif input_source.startswith("0x"):
        return "ximage"
    else:
        return "file"


def QUEUE(name, max_size_buffers=3, max_size_bytes=0, max_size_time=0, leaky="no"):
    return f"queue name={name} leaky={leaky} max-size-buffers={max_size_buffers} max-size-bytes={max_size_bytes} max-size-time={max_size_time} "


def get_camera_resulotion(video_width=640, video_height=640):
    if video_width <= 640 and video_height <= 480:
        return 640, 480
    elif video_width <= 1280 and video_height <= 720:
        return 1280, 720
    elif video_width <= 1920 and video_height <= 1080:
        return 1920, 1080
    else:
        return 3840, 2160


def SOURCE_PIPELINE(
    video_source,
    video_width=640,
    video_height=640,
    name="source",
    no_webcam_compression=False,
    frame_rate=20,
    sync=True,
    video_format="RGB",
):
    source_type = get_source_type(video_source)
    if source_type == "usb":
        if no_webcam_compression:
            source_element = (
                f"v4l2src device={video_source} name={name} ! "
                f"video/x-raw, width=640, height=480 ! "
                "videoflip name=videoflip video-direction=horiz ! "
            )
        else:
            width, height = get_camera_resulotion(video_width, video_height)
            source_element = (
                f"v4l2src device={video_source} name={name} ! image/jpeg, framerate=20/1, width={width}, height={height} ! "
                f'{QUEUE(name=f"{name}_queue_decode")} ! '
                f"decodebin name={name}_decodebin ! "
                f"videoflip name=videoflip video-direction=horiz ! "
            )
    elif source_type == "rpi":
        source_element = (
            f"appsrc name=app_source is-live=true leaky-type=downstream max-buffers=3 ! "
            "videoflip name=videoflip video-direction=horiz ! "
            f"video/x-raw, format={video_format}, width={video_width}, height={video_height} ! "
        )
    elif source_type == "libcamera":
        source_element = (
            f"libcamerasrc name={name} ! "
            f"video/x-raw, format={video_format}, width=1536, height=864 ! "
        )
    elif source_type == "ximage":
        source_element = (
            f"ximagesrc xid={video_source} ! "
            f'{QUEUE(name=f"{name}queue_scale_")} ! '
            f"videoscale ! "
        )
    elif source_type == "rtsp":
        source_element = (
            f"rtspsrc location={video_source} name=src_0 ! "
            "rtph264depay ! h264parse ! avdec_h264 max-threads=2 ! "
            "video/x-raw, format=I420 ! "
            "videorate ! video/x-raw, framerate=20/1 ! "
        )
    else:
        source_element = (
            f'filesrc location="{video_source}" name={name} ! '
            f'{QUEUE(name=f"{name}_queue_decode")} ! '
            f"decodebin name={name}_decodebin ! "
        )
    if sync:
        fps_caps = f"video/x-raw, framerate={frame_rate}/1"
    else:
        fps_caps = "video/x-raw"
    source_pipeline = (
        f"{source_element} "
        f'{QUEUE(name=f"{name}_scale_q")} ! '
        f"videoscale name={name}_videoscale n-threads=2 ! "
        f'{QUEUE(name=f"{name}_convert_q")} ! '
        f"videoconvert n-threads=3 name={name}_convert qos=false ! "
        f"video/x-raw, pixel-aspect-ratio=1/1, format={video_format}, "
        f"width={video_width}, height={video_height} ! "
        f'videorate name={name}_videorate ! capsfilter name={name}_fps_caps caps="{fps_caps}" '
    )
    return source_pipeline


def INFERENCE_PIPELINE(
    hef_path,
    post_process_so=None,
    batch_size=1,
    config_json=None,
    post_function_name=None,
    additional_params="",
    name="inference",
    scheduler_timeout_ms=None,
    scheduler_priority=None,
    vdevice_group_id=1,
    multi_process_service=None,
):
    config_str = f" config-path={config_json} " if config_json else ""
    function_name_str = (
        f" function-name={post_function_name} " if post_function_name else ""
    )
    vdevice_group_id_str = f" vdevice-group-id={vdevice_group_id} "
    multi_process_service_str = (
        f" multi-process-service={str(multi_process_service).lower()} "
        if multi_process_service is not None
        else ""
    )
    scheduler_timeout_ms_str = (
        f" scheduler-timeout-ms={scheduler_timeout_ms} "
        if scheduler_timeout_ms is not None
        else ""
    )
    scheduler_priority_str = (
        f" scheduler-priority={scheduler_priority} "
        if scheduler_priority is not None
        else ""
    )
    hailonet_str = (
        f"hailonet name={name}_hailonet "
        f"hef-path={hef_path} "
        f"batch-size={batch_size} "
        f"{vdevice_group_id_str}"
        f"{multi_process_service_str}"
        f"{scheduler_timeout_ms_str}"
        f"{scheduler_priority_str}"
        f"{additional_params} "
        f"force-writable=true "
    )
    inference_pipeline = (
        f'{QUEUE(name=f"{name}_scale_q")} ! '
        f"videoscale name={name}_videoscale n-threads=2 qos=false ! "
        f'{QUEUE(name=f"{name}_convert_q")} ! '
        f"video/x-raw, pixel-aspect-ratio=1/1 ! "
        f"videoconvert name={name}_videoconvert n-threads=2 ! "
        f'{QUEUE(name=f"{name}_hailonet_q")} ! '
        f"{hailonet_str} ! "
    )
    if post_process_so:
        inference_pipeline += (
            f'{QUEUE(name=f"{name}_hailofilter_q")} ! '
            f"hailofilter name={name}_hailofilter so-path={post_process_so} {config_str} {function_name_str} qos=false ! "
        )
    inference_pipeline += f'{QUEUE(name=f"{name}_output_q")} '
    return inference_pipeline


def INFERENCE_PIPELINE_WRAPPER(
    inner_pipeline, bypass_max_size_buffers=20, name="inference_wrapper"
):
    tappas_post_process_dir = os.environ.get(
        TAPPAS_POSTPROC_PATH_KEY, TAPPAS_POSTPROC_PATH_DEFAULT
    )
    whole_buffer_crop_so = os.path.join(
        tappas_post_process_dir, "cropping_algorithms/libwhole_buffer.so"
    )
    inference_wrapper_pipeline = (
        f'{QUEUE(name=f"{name}_input_q")} ! '
        f"hailocropper name={name}_crop so-path={whole_buffer_crop_so} function-name=create_crops use-letterbox=true resize-method=inter-area internal-offset=true "
        f"hailoaggregator name={name}_agg "
        f'{name}_crop. ! {QUEUE(max_size_buffers=bypass_max_size_buffers, name=f"{name}_bypass_q")} ! {name}_agg.sink_0 '
        f"{name}_crop. ! {inner_pipeline} ! {name}_agg.sink_1 "
        f'{name}_agg. ! {QUEUE(name=f"{name}_output_q")} '
    )
    return inference_wrapper_pipeline


def OVERLAY_PIPELINE(name="hailo_overlay"):
    return f'{QUEUE(name=f"{name}_q")} ! ' f"hailooverlay name={name} line-thickness=5"


def DISPLAY_PIPELINE(
    video_sink=GST_VIDEO_SINK, sync="true", show_fps="false", name="hailo_display"
):
    return (
        f'{OVERLAY_PIPELINE(name=f"{name}_overlay")} ! '
        f'{QUEUE(name=f"{name}_videoconvert_q")} ! '
        f"videoconvert name={name}_videoconvert n-threads=2 qos=false ! "
        f'{QUEUE(name=f"{name}_q")} ! '
        f"fpsdisplaysink name={name} video-sink={video_sink} sync={sync} text-overlay={show_fps} signal-fps-measurements=true "
    )


def USER_CALLBACK_PIPELINE(name="identity_callback"):
    return f'{QUEUE(name=f"{name}_q")} ! ' f"identity name={name} "


def TRACKER_PIPELINE(
    class_id,
    kalman_dist_thr=0.8,
    iou_thr=0.9,
    init_iou_thr=0.7,
    keep_new_frames=2,
    keep_tracked_frames=15,
    keep_lost_frames=2,
    keep_past_metadata=False,
    qos=False,
    name="hailo_tracker",
):
    return (
        f"hailotracker name={name} class-id={class_id} kalman-dist-thr={kalman_dist_thr} iou-thr={iou_thr} init-iou-thr={init_iou_thr} "
        f"keep-new-frames={keep_new_frames} keep-tracked-frames={keep_tracked_frames} keep-lost-frames={keep_lost_frames} keep-past-metadata={keep_past_metadata} qos={qos} ! "
        f'{QUEUE(name=f"{name}_q")} '
    )


def TEXT_OVERLAY_PIPELINE(
    name="text_overlay", font_size=12, halignment="center", valignment="top", color=None
):
    font_desc = f"Sans, {font_size}"
    color_property = f" color={hex(color)}" if color is not None else ""
    return (
        f'textoverlay name={name} font-desc="{font_desc}" halignment={halignment} valignment={valignment}{color_property} ! '
        f'{QUEUE(name=f"{name}_q")} '
    )
