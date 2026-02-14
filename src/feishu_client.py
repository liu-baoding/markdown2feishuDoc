import json
import os
import time
import io
from typing import List
import lark_oapi as lark
from lark_oapi.api.auth.v3 import *
from lark_oapi.api.drive.v1 import *
from lark_oapi.api.docx.v1 import *
from PIL import Image

from config.config import FEISHU_APP_ID, FEISHU_APP_SECRET, DEFAULT_PARENT_FOLDER_TOKEN
from src.markdown_parser import MarkdownParser


class FeishuClient:
    def __init__(self):
        self.app_id = FEISHU_APP_ID
        self.app_secret = FEISHU_APP_SECRET
        self.default_parent_folder_token = DEFAULT_PARENT_FOLDER_TOKEN

        # 初始化 SDK 客户端
        self.client = (
            lark.Client.builder()
            .app_id(self.app_id)
            .app_secret(self.app_secret)
            .log_level(lark.LogLevel.INFO)
            .build()
        )

        # 获取访问令牌
        self.access_token = self._get_access_token()

    def _get_access_token(self):
        """获取飞书访问令牌"""
        request: InternalTenantAccessTokenRequest = (
            InternalTenantAccessTokenRequest.builder()
            .request_body(
                InternalTenantAccessTokenRequestBody.builder()
                .app_id(self.app_id)
                .app_secret(self.app_secret)
                .build()
            )
            .build()
        )
        resp: InternalTenantAccessTokenResponse = self.client.auth.v3.tenant_access_token.internal(request)
        if resp.code != 0:
            print(f"[DEBUG] 获取访问令牌失败: code={resp.code}, msg={resp.msg}")
            raise Exception(f"获取访问令牌失败: code={resp.code}, msg={resp.msg}")

        return json.loads(resp.raw.content).get("tenant_access_token")

    def create_folder(self, folder_name, parent_token=None):
        """创建飞书云文档文件夹
        Args:
            folder_name: 文件夹名称
            parent_token: 父文件夹的 token，如果为 None 则创建在根目录
        Returns:
            str: 创建的文件夹 token
        """
        folder_name = folder_name.rsplit(" ", 1)[0]  # 从右侧按空格拆分一次，取第一部分

        req = (
            CreateFolderFileRequest.builder()
            .request_body(
                CreateFolderFileRequestBody.builder()
                .name(folder_name)
                .folder_token(parent_token if parent_token else "")
                .build()
            )
            .build()
        )

        resp: CreateFolderFileResponse = self.client.drive.v1.file.create_folder(req)
        if resp.code != 0:
            raise Exception(f"创建文件夹失败: {resp}")

        return resp.data.token

    def _upload_md_to_cloud(self, title, file_size, folder_token, md_content_bytes) -> str:
        """md文件导入飞书文档"""
        print(f"[DEBUG] 开始上传MD文件")
        print(f"[DEBUG] 文件名: {title}.md")
        print(f"[DEBUG] 声明大小: {file_size} bytes")
        print(f"[DEBUG] 目标文件夹token: {folder_token}")

        # 将 bytes 包装成流对象，SDK 对流对象的兼容性更好
        md_stream = io.BytesIO(md_content_bytes)

        file_req: UploadAllFileRequest = (
            UploadAllFileRequest.builder()
            .request_body(
                UploadAllFileRequestBody.builder()
                .file_name(title + ".md")
                .parent_type("explorer")
                .parent_node(folder_token)
                .size(file_size)
                .file(md_stream)
                .build()
            )
            .build()
        )

        file_resp: UploadAllFileResponse = self.client.drive.v1.file.upload_all(file_req)

        # 打印详细响应信息
        print(f"[DEBUG] 响应code: {file_resp.code}")
        print(f"[DEBUG] 响应msg: {file_resp.msg}")
        if hasattr(file_resp, "raw") and file_resp.raw:
            print(f"[DEBUG] 原始响应: {file_resp.raw.content}")

        if file_resp.code != 0:
            raise Exception(f"上传md文件失败: code={file_resp.code}, msg={file_resp.msg}")
        return file_resp.data.file_token

    def _create_import_task(self, file_token, title, folder_token) -> str:
        """创建md文件导入为云文档任务
        args:
            file_token: md文件的token
            title: 文档标题
            folder_token: 文档所在文件夹的token
        returns:
            ticket: 导入任务的ticket
        """
        # 创建md文件导入为云文档
        import_req: CreateImportTaskRequest = (
            CreateImportTaskRequest.builder()
            .request_body(
                ImportTask.builder()
                .file_extension("md")
                .file_token(file_token)
                .type("docx")
                .file_name(title)
                .point(ImportTaskMountPoint.builder().mount_type(1).mount_key(folder_token).build())
                .build()
            )
            .build()
        )

        import_resp: CreateImportTaskResponse = self.client.drive.v1.import_task.create(import_req)
        if import_resp.code != 0:
            print(f"[DEBUG] 创建导入任务失败: code={import_resp.code}, msg={import_resp.msg}")
            raise Exception(f"创建导入任务失败: code={import_resp.code}, msg={import_resp.msg}")
        return import_resp.data.ticket

    def _get_import_docx_token(self, ticket) -> str:
        """轮询导入任务状态，获取导入文档的token
        args:
            ticket: 导入任务的ticket
        returns:
            docx_token: 导入文档的token
        """
        request: GetImportTaskRequest = GetImportTaskRequest.builder().ticket(ticket).build()

        while True:
            response: GetImportTaskResponse = self.client.drive.v1.import_task.get(request)
            if response.code != 0:
                print(f"[DEBUG] 获取导入任务状态失败: code={response.code}, msg={response.msg}")
                raise Exception(f"获取导入任务状态失败: code={response.code}, msg={response.msg}")

            job_status = response.data.result.job_status
            if job_status == 2:  # 处理成功
                # [核心修正] 针对 MD 导入 Docx 存在的异步延迟问题，增加重试获取 token 机制
                # 任务刚成功时 token 可能尚未就绪，先等待 5 秒
                time.sleep(5)
                doc_token = None
                retry_count = 0
                while retry_count < 5:
                    raw_content = response.raw.content.decode("utf-8") if hasattr(response, "raw") else "{}"
                    result = response.data.result

                    # 尝试多种路径获取 token
                    doc_token = getattr(result, "token", None) or getattr(result, "file_token", None)
                    if not doc_token:
                        try:
                            resp_json = json.loads(raw_content)
                            res_data = resp_json.get("data", {}).get("result", {})
                            doc_token = (
                                res_data.get("token")
                                or res_data.get("file_token")
                                or res_data.get("obj_token")
                            )
                            if not doc_token and res_data.get("url"):
                                doc_token = res_data.get("url").split("/")[-1].split("?")[0]
                        except:
                            pass

                    if doc_token:
                        break

                    print(f"[DEBUG] 任务成功但未检测到 token，等待 2s 后重试 ({retry_count + 1}/5)...")
                    time.sleep(2)
                    response = self.client.drive.v1.import_task.get(request)
                    retry_count += 1

                print(
                    f"[DEBUG] 导入任务最终响应内容: {response.raw.content.decode('utf-8') if hasattr(response, 'raw') else 'None'}"
                )
                print(f"[DEBUG] 导入文档成功, doc_token: {doc_token}")
                return doc_token
            elif job_status == 0 or job_status == 1:  # 初始化或处理中
                print("任务处理中...")
            else:  # job_status == 3，处理失败
                raise Exception(f"任务处理失败：{response.data.result.job_error_msg}")

            # 等待一段时间后再次查询状态
            time.sleep(2)

    def import_md_to_docx(self, file_path, title, folder_token):
        """md文件导入飞书文档"""
        # 初始化记录，用于失败后的清理
        uploaded_md_token = None
        created_doc_token = None

        try:
            # 1. 文本模式读取：仅用于解析图片路径
            with open(file_path, "r", encoding="utf-8") as f:
                md_text = f.read()

            # 2. 将归一化后的文本转回字节流，确保大小一致
            md_content_normalized = md_text.encode("utf-8")
            real_file_size = len(md_content_normalized)

            # 提取出markdown的所有图片路径
            img_path_list: List = MarkdownParser.extract_images_from_markdown(file_path, md_text)

            # 3. 上传md文件, 获取file_token
            uploaded_md_token = self._upload_md_to_cloud(
                title, real_file_size, folder_token, md_content_normalized
            )

            # 4. 创建md文件导入为云文档, 获取ticket
            ticket = self._create_import_task(uploaded_md_token, title, folder_token)

            # 5. 轮询导入任务状态，获取导入文档的token
            created_doc_token = self._get_import_docx_token(ticket)

            # 6. 把markdown中记录的图片路径，上传图片到飞书文档，更新image block of the image_key
            if img_path_list:
                self._update_document_images(created_doc_token, img_path_list)

            # 7. 任务成功，删除上传的中间态 md 文件
            self._del_file(uploaded_md_token)

        except Exception as e:
            print(f"[ERROR] 迁移文档 '{title}' 时发生错误: {str(e)}")
            # 失败补救：清理飞书上的残留文件
            print(f"[DEBUG] 正在尝试清理由于错误产生的飞书残留文件...")

            if uploaded_md_token:
                try:
                    self._del_file(uploaded_md_token)
                    print(f"  - 已清理残留 MD 文件: {uploaded_md_token}")
                except:
                    pass

            if created_doc_token:
                try:
                    # 飞书云文档新版(docx)删除时类型必须指定为 'docx'
                    self._del_file(created_doc_token, file_type="docx")
                    print(f"  - 已清理残留 Doc 文档: {created_doc_token}")
                except:
                    pass

            # 重新抛出异常，让主流程感知失败
            raise e

    def _update_document_images(self, doc_token, img_path_list: List):
        """更新文档中的图片
        Args:
            doc_token: 文档token
            ima_path_list: markdown中记录的图片地址列表
        """
        print(f"[DEBUG] 开始获取文档块, doc_token: {doc_token}")
        # 获取文档所有块
        request: ListDocumentBlockRequest = (
            ListDocumentBlockRequest.builder().document_id(doc_token).page_size(500).build()
        )
        # 访问img_path_list的索引位置
        img_path_index = 0

        while True:
            resp: ListDocumentBlockResponse = self.client.docx.v1.document_block.list(request)
            if resp.code != 0:
                print(f"[DEBUG] 获取文档块失败: code={resp.code}, msg={resp.msg}")
                if hasattr(resp, "raw") and resp.raw:
                    print(f"[DEBUG] 原始响应: {resp.raw.content}")
                raise Exception(f"获取文档块失败: code={resp.code}, msg={resp.msg}")

            # 遍历所有块
            for block in resp.data.items:
                # 检查是否为图片块
                if block.block_type == 27 and img_path_index < len(img_path_list):  # 图片块
                    # 上传图片到飞书文档指定的 block 中
                    img_path = img_path_list[img_path_index]
                    print(f"[DEBUG] [IMAGE_STEP] 正在处理第 {img_path_index + 1} 张图片: {img_path}")

                    try:
                        image_token = self._upload_image_to_doc(img_path, block.block_id, doc_token)
                        # 更新图片块的image_key
                        self._update_doc_image_block(img_path, block.block_id, doc_token, image_token)
                        img_path_index += 1

                        # [频率控制] 避免请求过快触发飞书 API 限制
                        time.sleep(1)
                    except Exception as img_err:
                        print(f"[DEBUG] [IMAGE_ERROR] 处理图片时发生错误: {str(img_err)}")
                        raise img_err

            # 检查是否还有更多块
            if not resp.data.has_more:
                break

            # 更新请求参数，获取下一页
            request.page_token = resp.data.page_token

    def _upload_image_to_doc(self, file_path, block_id, document_id):
        """上传图片到飞书文档，带重试机制"""
        file_name = os.path.basename(file_path)
        file_size = os.path.getsize(file_path)

        max_retries = 3
        for attempt in range(max_retries):
            try:
                with open(file_path, "rb") as image_content:
                    extra: dict = {"drive_route_token": document_id}
                    request: UploadAllMediaRequest = (
                        UploadAllMediaRequest.builder()
                        .request_body(
                            UploadAllMediaRequestBody.builder()
                            .file_name(file_name)
                            .parent_node(block_id)
                            .parent_type("docx_image")
                            .size(file_size)
                            .extra(json.dumps(extra, ensure_ascii=False, indent=2))
                            .file(image_content)
                            .build()
                        )
                        .build()
                    )

                    print(
                        f"[DEBUG] [API_CALL] 开始调用 media.upload_all (尝试 {attempt + 1}/{max_retries})..."
                    )
                    resp: UploadAllMediaResponse = self.client.drive.v1.media.upload_all(request)

                    if resp.code != 0:
                        # 如果是频率限制或其他可重试错误，可以在此判断
                        print(f"[DEBUG] 上传图片到云文档失败: code={resp.code}, msg={resp.msg}")
                        if attempt < max_retries - 1:
                            wait_time = (attempt + 1) * 2
                            print(f"[DEBUG] 等待 {wait_time}s 后重试...")
                            time.sleep(wait_time)
                            continue
                        raise Exception(f"上传图片到云文档失败: code={resp.code}, msg={resp.msg}")

                    print(f"上传图片到云文档成功: {resp.data.file_token}")
                    return resp.data.file_token
            except Exception as e:
                # 捕获 JSON 解析错误或其他网络异常请求
                if "Expecting value" in str(e) or "char 0" in str(e):
                    print(f"[DEBUG] [NETWORK_ISSUE] 捕获到可能的空响应错误: {str(e)}")
                    if attempt < max_retries - 1:
                        wait_time = (attempt + 1) * 2
                        print(f"[DEBUG] 服务器返回异常或网络抖动，等待 {wait_time}s 后重试...")
                        time.sleep(wait_time)
                        continue
                raise e

    def _update_doc_image_block(self, file_path, block_id, document_id, image_token):
        """更新文档中的图片块，带重试机制"""
        # 获取图片尺寸
        with Image.open(file_path) as img:
            width, height = img.size
            print(f"图片尺寸: {width}x{height}")

        max_retries = 3
        for attempt in range(max_retries):
            try:
                # 更新图片块的image_key
                request: PatchDocumentBlockRequest = (
                    PatchDocumentBlockRequest.builder()
                    .document_id(document_id)
                    .block_id(block_id)
                    .request_body(
                        UpdateBlockRequest.builder()
                        .replace_image(
                            ReplaceImageRequest.builder()
                            .token(image_token)
                            .width(width)
                            .height(height)
                            .build()
                        )
                        .build()
                    )
                    .build()
                )

                print(
                    f"[DEBUG] [API_CALL] 开始调用 document_block.patch (尝试 {attempt + 1}/{max_retries})..."
                )
                # 发起请求
                response: PatchDocumentBlockResponse = self.client.docx.v1.document_block.patch(request)
                if response.code != 0:
                    print(f"[DEBUG] 更新图片块失败: code={response.code}, msg={response.msg}")
                    if attempt < max_retries - 1:
                        wait_time = (attempt + 1) * 2
                        time.sleep(wait_time)
                        continue
                    raise Exception(f"更新图片块失败: code={response.code}, msg={response.msg}")

                print("更新图片块成功")
                return
            except Exception as e:
                if "Expecting value" in str(e) or "char 0" in str(e):
                    if attempt < max_retries - 1:
                        wait_time = (attempt + 1) * 2
                        print(f"[DEBUG] 网络异常，等待 {wait_time}s 后重试...")
                        time.sleep(wait_time)
                        continue
                raise e

    def _del_file(self, file_token, file_type="file"):
        """删除文件
        Args:
            file_token: 文件 token
            file_type: 文件类型 (file, docx, bitable, folder, etc.)
        """
        request: DeleteFileRequest = (
            DeleteFileRequest.builder().file_token(file_token).type(file_type).build()
        )
        resp: DeleteFileResponse = self.client.drive.v1.file.delete(request)
        if resp.code != 0:
            print(f"[DEBUG] 删除文件失败: code={resp.code}, msg={resp.msg}")
            raise Exception(f"删除文件失败: code={resp.code}, msg={resp.msg}")
        print(f"删除文件成功, token: {file_token}, type: {file_type}")
